#!/usr/bin/env python3
"""tools/triage.py - I/O around the ticket triage engine (tools/engine.py).

    python3 tools/triage.py run                  # triage every open ticket, queue for review
    python3 tools/triage.py list [--status open]
    python3 tools/triage.py complete <ticket_id>  # fast-forward: engineer finished the job

`compute()` is the pure-ish core other tools share: it loads open tickets and
the PMS's VIP context and calls `engine.triage_tickets()`. It never writes
anything, so `tools/run.py` and `tools/routes.py` (which needs today's
high-priority tickets to interleave into the cleaning routes) both call it
once per pass instead of triaging twice.

Nothing here writes a ticket's final assignee/trade/eta until a human has
approved the run - see `apply_triage_run()`, called only from
`tools/review.py send`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_messaging, get_pms  # noqa: E402
from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.llm import LLMResult, complete  # noqa: E402
from core.review import assert_write_allowed  # noqa: E402
from core.store import Item, Store, StoreError  # noqa: E402
from core.templates import build_prompt  # noqa: E402

import engine  # noqa: E402
import store_ext  # noqa: E402

SCHEMAS_DIR = REPO_ROOT / "prompts" / "schemas"
TRIAGE_NOTE_SCHEMA = json.loads((SCHEMAS_DIR / "triage-note.json").read_text(encoding="utf-8"))


def _rules(settings: Settings) -> dict:
    return dict(settings.agent_get("rules", {}) or {})


def vip_names(settings: Settings) -> set[str]:
    """Lower-cased full names of guests the PMS confirms are VIP.

    Best-effort: a stub/broken PMS or an empty fixture just yields an empty
    set, so the VIP-arrival escalation rule quietly does not fire instead of
    crashing the run - see docs/how-it-works.md "Design decisions".

    A reservation's own ``guest.vip`` is trusted first - the `mock` and
    `cloudbeds` adapters set it there directly. The `csv` adapter cannot:
    its `reservations.csv` row has no vip column of its own
    (`core/adapters/pms_csv.py:_to_reservation()` never reads one) - only
    `guests.csv` does, through `find_guest`/`get_guest`
    (`core/adapters/pms_csv.py:_to_guest()`). So for a reservation whose own
    guest reports `vip=False`, resolve the guest by email (falling back to
    name) through the PMS's guest lookup and trust that record's vip flag
    too, whenever the PMS can look guests up at all. See
    docs/integrations.md "PMS" for the CSV columns this expects.
    """
    try:
        pms = get_pms(settings)
        reservations = pms.list_reservations("1900-01-01", "2999-12-31")
    except Exception:  # noqa: BLE001 - a missing/broken PMS must not stop triage
        return set()
    try:
        can_look_up_guests = "find_guest" in pms.capabilities()
    except Exception:  # noqa: BLE001 - capabilities() itself must not stop triage
        can_look_up_guests = False

    names: set[str] = set()
    for r in reservations:
        if not r.guest.full_name:
            continue
        vip = r.guest.vip
        if not vip and can_look_up_guests:
            try:
                matches = pms.find_guest(email=r.guest.email) if r.guest.email else []
                if not matches:
                    matches = pms.find_guest(name=r.guest.full_name)
            except Exception:  # noqa: BLE001 - a broken lookup must not stop triage
                matches = []
            vip = any(g.vip for g in matches)
        if vip:
            names.add(r.guest.full_name.lower())
    return names


def open_tickets(store: Store, ticket_source: str = "auto") -> list[engine.Ticket]:
    """Every open ticket, seeding from fixtures/imports on first call.

    ``ticket_source`` is passed straight through to
    ``store_ext.seed_tickets``/``load_seed_tickets`` - ``"auto"`` (default)
    for a real pass, ``"fixtures"`` for ``tools/demo.py`` so the demo never
    reads a hotel's own ``data/imports/maintenance_tickets.csv``.
    """
    store_ext.seed_tickets(store, source=ticket_source)
    return [engine.Ticket(id=t.id, room=t.room, summary=t.summary, detail=t.detail,
                          priority=t.priority)
           for t in store_ext.list_tickets(store, status="open")]


def compute(settings: Settings, store: Store, ticket_source: str = "auto") -> engine.TriageResult:
    """Load open tickets + VIP context and run the deterministic engine.

    No writes. Safe to call more than once per pass. See ``open_tickets``
    for ``ticket_source``.
    """
    tickets = open_tickets(store, ticket_source=ticket_source)
    return engine.triage_tickets(tickets, rules=_rules(settings), config=settings.agent,
                                 vip_names=vip_names(settings))


def high_priority_tickets(result: engine.TriageResult) -> list[engine.Ticket]:
    """The subset `tools/routes.py` needs to interleave into today's routes."""
    return [engine.Ticket(id=d.ticket_id, room=d.room, summary=d.summary)
           for d in result.decisions if d.priority == "high"]


def _engineer_name(settings: Settings, engineer_key: str) -> str:
    engineers = settings.agent_get("engineers", {}) or {}
    return str(engineers.get(engineer_key, engineer_key.title()))


def _result_to_dict(settings: Settings, result: engine.TriageResult) -> dict:
    return {
        "escalated_count": result.escalated_count,
        "contractor_held_count": result.contractor_held_count,
        "low_priority_count": result.low_priority_count,
        "thinking_log": result.thinking_log,
        "decisions": [
            {
                "ticket_id": d.ticket_id, "room": d.room, "summary": d.summary,
                "trade": d.trade,
                "assignee": "Contractor" if d.contractor else _engineer_name(settings, d.engineer_key),
                "priority": d.priority, "upgraded": d.upgraded, "reason": d.reason,
                "minutes": d.minutes, "parts_cost": d.parts_cost, "parts_note": d.parts_note,
                "lead_time_note": d.lead_time_note, "contractor": d.contractor,
                "held_for_signoff": d.held_for_signoff, "schedule_label": d.schedule_label,
            }
            for d in result.decisions
        ],
    }


def build_triage_run(settings: Settings, store: Store, result: engine.TriageResult, *,
                     provider: str | None = None) -> tuple[Item, bool]:
    """Write today's triage run as one review item, with its narrative note.

    Idempotent per calendar day: a second call the same day returns the
    existing item untouched (``item.intent`` is only set once the run has
    actually been computed).
    """
    today = date.today().isoformat()
    item = store.upsert_item("housekeeping-triage", today, kind="triage_run",
                             payload={"date": today})
    if item.intent:
        return item, False

    payload = _result_to_dict(settings, result)
    prompt = build_prompt("triage-note", settings=settings, item=payload)
    llm_result: LLMResult = complete("triage-note", prompt, TRIAGE_NOTE_SCHEMA,
                                     settings=settings, provider=provider, store=store,
                                     item_id=item.id, fixture_id="triage-0")
    narrative = llm_result.data or {}
    store.set_fields(item.id, intent="triaged", draft={"result": payload, "narrative": narrative})
    needs_human = result.contractor_held_count > 0 or any(
        d["upgraded"] for d in payload["decisions"])
    status = "needs_human" if needs_human else "pending_review"
    updated = store.transition(item.id, status, actor="agent",
                               detail={"tickets": len(result.decisions)})
    return updated, True


def apply_triage_run(settings: Settings, store: Store, item: Item) -> dict:
    """Called by ``tools/review.py send`` once the run is approved/edited.

    Writes every ticket's assignee/trade/eta/parts_note (``status`` moves to
    ``triaged``), then tells the engineering team what is waiting for them.

    The ticket-status write itself is not an adapter call (it is this
    agent's own `hk_tickets` table, `tools/store_ext.py`), so nothing
    upstream guards it automatically the way `@guarded_write` guards a PMS
    or sheets write. Ask the guard explicitly before touching a single row,
    so `mode: shadow` blocks it exactly like the CSV export and the staff
    notification below do - see docs/safety.md.
    """
    assert_write_allowed(settings, "pms_write", item)
    result = (item.draft or {}).get("result", {})
    written = 0
    for decision in result.get("decisions", []):
        store_ext.apply_triage(store, decision["ticket_id"], priority=decision["priority"],
                               assignee=decision["assignee"], trade=decision["trade"],
                               eta=decision["schedule_label"], parts_note=decision["parts_note"],
                               ai_triage=decision)
        written += 1
    messaging = get_messaging(settings)
    note = (item.draft or {}).get("narrative", {}).get("note", "")
    if note:
        messaging.notify_staff(f"Maintenance triage: {note}", item=item)
    return {"message_id": f"triage-{item.payload.get('date', '')}", "tickets_written": written}


def complete_ticket(store: Store, ticket_id: str) -> store_ext.TicketRow | None:
    """Fast-forward: the engineer finished the job.

    No review needed - this records something that already happened in the
    real world, and releases the room on the shared board.
    """
    return store_ext.complete_ticket(store, ticket_id)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def cmd_run(store: Store, settings: Settings, args: argparse.Namespace) -> int:
    result = compute(settings, store)
    item, did_work = build_triage_run(settings, store, result, provider=args.provider)
    if not did_work:
        print(f"Today's triage run was already computed: {item.id} ({item.review_status}).")
        return 0
    for line in result.thinking_log:
        print(f"  {line}")
    print(f"\n{len(result.decisions)} ticket(s) triaged - item {item.id}, "
         f"status {item.review_status}.")
    print("Run `make review` to see it, then `python3 tools/review.py approve "
         f"{item.id}` and `python3 tools/review.py send` to apply it.")
    return 0


def cmd_list(store: Store, args: argparse.Namespace) -> int:
    store_ext.seed_tickets(store)
    tickets = store_ext.list_tickets(store, status=args.status)
    if not tickets:
        print(f"No tickets with status '{args.status or 'any'}'.")
        return 0
    for t in tickets:
        print(f"  {t.id:<14} {t.status:<9} {t.priority:<7} {t.room:<16} {t.summary}")
    return 0


def cmd_complete(store: Store, args: argparse.Namespace) -> int:
    ticket = store_ext.get_ticket(store, args.ticket_id)
    if ticket is None:
        print(f"error: no ticket {args.ticket_id}", file=sys.stderr)
        return 1
    complete_ticket(store, args.ticket_id)
    print(f"{args.ticket_id} ({ticket.room}) marked done - room released on the board.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="triage every open ticket and queue the run for review")
    p_run.add_argument("--provider", default=None)

    p_list = sub.add_parser("list", help="show tickets")
    p_list.add_argument("--status", default=None, help="open | triaged | done")

    p_complete = sub.add_parser("complete", help="fast-forward: engineer finished the job")
    p_complete.add_argument("ticket_id")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    store_ext.ensure_schema(store)
    try:
        if args.command == "run":
            return cmd_run(store, settings, args)
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "complete":
            return cmd_complete(store, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
