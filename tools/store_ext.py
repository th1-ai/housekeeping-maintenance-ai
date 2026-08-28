"""tools/store_ext.py - The Steward's own tables, layered on core.store.Store.

The generic `items` table (core/store.py) is the review queue: one row per
route plan or triage run waiting on a human. It is not a ticket ledger. This
module adds the two tables a hotel actually needs to query - open/triaged
maintenance tickets and the Locksmith's key-issue audit feed - plus the
loaders for the room board and the seeded tickets.

Call :func:`ensure_schema` once per `Store` right after constructing it;
every tool in this repo does it. Nothing here replaces `core.store` - it is
additive, using the same connection (`store.db`) and the same `utcnow()`
timestamp convention core.store itself uses.
"""

from __future__ import annotations

import csv
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import repo_root, sub_data_dir
from core.store import Store, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS hk_tickets (
  id           TEXT PRIMARY KEY,
  room         TEXT NOT NULL,
  summary      TEXT NOT NULL,
  detail       TEXT,
  priority     TEXT NOT NULL DEFAULT 'medium',
  status       TEXT NOT NULL DEFAULT 'open',
  source       TEXT NOT NULL DEFAULT 'seed',
  assignee     TEXT,
  trade        TEXT,
  eta          TEXT,
  parts_note   TEXT,
  ai_triage_json TEXT,
  item_id      TEXT,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hk_tickets_status ON hk_tickets (status, priority);

CREATE TABLE IF NOT EXISTS hk_lock_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  room       TEXT NOT NULL,
  kind       TEXT NOT NULL,
  detail     TEXT,
  actor      TEXT NOT NULL DEFAULT 'The Locksmith'
);
"""


def ensure_schema(store: Store) -> None:
    """Create both tables above if they do not already exist. Idempotent."""
    store.migrate(SCHEMA)


# --------------------------------------------------------------------------
# tickets
# --------------------------------------------------------------------------
@dataclass
class TicketRow:
    """One row of ``hk_tickets``, as read back for the triage engine and CLI."""

    id: str
    room: str
    summary: str
    detail: str = ""
    priority: str = "medium"
    status: str = "open"
    source: str = "seed"
    assignee: str = ""
    trade: str = ""
    eta: str = ""
    parts_note: str = ""
    ai_triage: dict = field(default_factory=dict)
    item_id: str = ""
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: Any) -> "TicketRow":
        raw = row["ai_triage_json"]
        return cls(
            id=row["id"], room=row["room"], summary=row["summary"],
            detail=row["detail"] or "", priority=row["priority"], status=row["status"],
            source=row["source"], assignee=row["assignee"] or "", trade=row["trade"] or "",
            eta=row["eta"] or "", parts_note=row["parts_note"] or "",
            ai_triage=json.loads(raw) if raw else {}, item_id=row["item_id"] or "",
            created_at=row["created_at"], updated_at=row["updated_at"])


def seed_tickets(store: Store, source: str = "auto") -> int:
    """Insert every fixture/imported ticket that is not already in the table.

    Idempotent on ``id`` - safe to call on every ``make demo`` / ``tools/run.py``
    pass. Returns the number of new rows inserted. ``source`` is passed
    straight through to :func:`load_seed_tickets` - see its docstring.
    """
    inserted = 0
    for raw in load_seed_tickets(source=source):
        existing = store.db.execute("SELECT id FROM hk_tickets WHERE id=?",
                                    (raw["id"],)).fetchone()
        if existing is not None:
            continue
        now = utcnow()
        store.db.execute(
            "INSERT INTO hk_tickets (id, room, summary, detail, priority, status, "
            "source, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (raw["id"], raw["room"], raw["summary"], raw.get("detail", ""),
             raw.get("priority", "medium"), "open", raw.get("source", "seed"), now, now))
        inserted += 1
    return inserted


def create_ticket(store: Store, *, room: str, summary: str, detail: str = "",
                  priority: str = "medium", source: str = "guest_email",
                  item_id: str = "") -> TicketRow:
    """Insert a new ticket (from a guest-message detection) and return it."""
    ticket_id = f"guest-{uuid.uuid4().hex[:10]}"
    now = utcnow()
    store.db.execute(
        "INSERT INTO hk_tickets (id, room, summary, detail, priority, status, source, "
        "item_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ticket_id, room, summary, detail, priority, "open", source, item_id, now, now))
    return get_ticket(store, ticket_id)


def get_ticket(store: Store, ticket_id: str) -> TicketRow | None:
    row = store.db.execute("SELECT * FROM hk_tickets WHERE id=?", (ticket_id,)).fetchone()
    return TicketRow.from_row(row) if row else None


def list_tickets(store: Store, *, status: str | None = None) -> list[TicketRow]:
    if status:
        rows = store.db.execute(
            "SELECT * FROM hk_tickets WHERE status=? ORDER BY created_at ASC",
            (status,)).fetchall()
    else:
        rows = store.db.execute("SELECT * FROM hk_tickets ORDER BY created_at ASC").fetchall()
    return [TicketRow.from_row(r) for r in rows]


def apply_triage(store: Store, ticket_id: str, *, priority: str, assignee: str, trade: str,
                 eta: str, parts_note: str, ai_triage: dict) -> TicketRow:
    """Write a ticket's triage result. Only called once its item is approved.

    ``priority`` is the post-escalation priority (``ai_triage["priority"]``) -
    this is the one field that can change what the ticket started at, so an
    upgraded ticket reads as upgraded everywhere, not just inside
    ``ai_triage_json``.
    """
    store.db.execute(
        "UPDATE hk_tickets SET status='triaged', priority=?, assignee=?, trade=?, eta=?, "
        "parts_note=?, ai_triage_json=?, updated_at=? WHERE id=?",
        (priority, assignee, trade, eta, parts_note,
         json.dumps(ai_triage, ensure_ascii=False), utcnow(), ticket_id))
    return get_ticket(store, ticket_id)


def complete_ticket(store: Store, ticket_id: str) -> TicketRow:
    """Fast-forward: the engineer finished the job. Releases the room on the board."""
    store.db.execute("UPDATE hk_tickets SET status='done', updated_at=? WHERE id=?",
                     (utcnow(), ticket_id))
    return get_ticket(store, ticket_id)


# --------------------------------------------------------------------------
# lock events (The Locksmith)
# --------------------------------------------------------------------------
def record_lock_event(store: Store, *, room: str, kind: str, detail: str = "",
                      actor: str = "The Locksmith") -> int:
    now = utcnow()
    cur = store.db.execute(
        "INSERT INTO hk_lock_events (created_at, room, kind, detail, actor) "
        "VALUES (?,?,?,?,?)", (now, room, kind, detail, actor))
    return int(cur.lastrowid)


def lock_feed(store: Store, limit: int = 60) -> list[dict]:
    rows = store.db.execute(
        "SELECT * FROM hk_lock_events ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# room board + seeded tickets: CSV import first, fixtures fallback
# --------------------------------------------------------------------------
def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_room_board(source: str = "auto") -> list[dict]:
    """Today's room board.

    ``source="auto"`` (the default, used by ``tools/run.py`` and the
    ``tools/routes.py``/``tools/triage.py`` CLIs) looks for
    ``data/imports/room_status.csv`` first (a real property's PMS export -
    see docs/integrations.md), then falls back to
    ``fixtures/hotel/room_status.json``.

    ``source="fixtures"`` skips the import CSV entirely, even when one
    exists, and reads only the bundled fixture. ``tools/demo.py`` always
    passes this - ``make demo`` is a fixed, reproducible scenario ("Hotel
    Aurora, day 0") and must show the same thing whether or not a hotel has
    already filled in its own ``data/imports/*.csv`` (SIMULATION.md Round 2,
    finding B).
    """
    if source not in ("auto", "fixtures"):
        raise ValueError(f"unknown source '{source}', expected 'auto' or 'fixtures'")
    csv_path = sub_data_dir("imports") / "room_status.csv"
    if source == "auto" and csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        return [{
            "room_number": str(r.get("room_number") or r.get("room") or "").strip(),
            "floor": int(r.get("floor") or 0),
            "room_type": str(r.get("room_type") or ""),
            "status": str(r.get("status") or "vacant").strip().lower(),
            "vip": str(r.get("vip") or "").strip().lower() in ("1", "true", "yes"),
            "note": str(r.get("note") or ""),
        } for r in rows if r.get("room_number") or r.get("room")]
    fixture = repo_root() / "fixtures" / "hotel" / "room_status.json"
    if not fixture.exists():
        return []
    return _read_json(fixture).get("rooms", [])


def load_seed_tickets(source: str = "auto") -> list[dict]:
    """Tickets to seed on first run.

    Same ``source`` contract as :func:`load_room_board`: ``"auto"`` (default)
    is CSV import first, fixtures fallback; ``"fixtures"`` (what
    ``tools/demo.py`` always passes) ignores any ``data/imports/*.csv`` and
    reads only the bundled fixture, so ``make demo`` stays the same fixed
    scenario regardless of what a hotel has imported.
    """
    if source not in ("auto", "fixtures"):
        raise ValueError(f"unknown source '{source}', expected 'auto' or 'fixtures'")
    csv_path = sub_data_dir("imports") / "maintenance_tickets.csv"
    if source == "auto" and csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
        return [{
            "id": str(r.get("id") or uuid.uuid4().hex[:8]),
            "room": str(r.get("room") or ""),
            "summary": str(r.get("summary") or ""),
            "detail": str(r.get("detail") or ""),
            "priority": str(r.get("priority") or "medium"),
            "source": "csv_import",
        } for r in rows if r.get("room")]
    fixture = repo_root() / "fixtures" / "hotel" / "maintenance_tickets.json"
    if not fixture.exists():
        return []
    return _read_json(fixture).get("tickets", [])
