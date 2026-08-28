#!/usr/bin/env python3
"""tools/demo.py - the whole loop on the bundled fixtures, zero credentials.

    make demo
    python3 tools/demo.py

Forces `llm.provider=mock`, `mode=shadow` and the `mock` adapter for every
system (`load_settings(demo=True)`) regardless of what config/hotel.yaml
has - so this always works on a fresh clone with a blank .env, and never
reaches a real mailbox or PMS even once a hotel has pointed those at
something real. Runs against its own database (data/demo/demo.db) so
running it twice always shows the same result, and never touches
data/agent.db (that is `make run`'s file).

Walks all four things The Steward does: checks four sample guest emails for a
maintenance issue, triages every open ticket, builds today's cleaning routes
with that triage folded in, and runs the Locksmith's issue/revoke feed.
Nothing is dispatched or sent - mode is shadow and demo never calls
tools/review.py send.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.config import ConfigError, load_settings, sub_data_dir  # noqa: E402
from core.log import summary_line  # noqa: E402
from core.store import Store  # noqa: E402

import routes  # noqa: E402
import store_ext  # noqa: E402
import ticket_intake  # noqa: E402
import triage  # noqa: E402


def main() -> int:
    try:
        settings = load_settings(demo=True)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    demo_db = sub_data_dir("demo") / "demo.db"
    if demo_db.exists():
        demo_db.unlink()
    store = Store(settings, path=demo_db)
    store_ext.ensure_schema(store)

    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0, "tickets_logged": 0}

    print("The Steward demo - Hotel Aurora, day 0\n")
    print("1) Checking guest email for maintenance issues (fixtures/inbound/*.json)")
    email = get_email(settings)
    messages = email.fetch_unread(limit=50)
    for msg in messages:
        item, _, ticket = ticket_intake.process_email(settings, store, msg, provider="mock")
        if ticket is not None:
            stats["tickets_logged"] += 1
            print(f"  {msg.id}: \"{msg.subject}\" -> ticket {ticket.id} logged for room "
                 f"{ticket.room} (confidence set by the model, see prompts/ticket-detect.md)")
        elif item.review_status == "needs_human":
            stats["needs_human"] += 1
            print(f"  {msg.id}: \"{msg.subject}\" -> needs_human "
                 "(low-confidence maintenance detection)")
        else:
            print(f"  {msg.id}: \"{msg.subject}\" -> not a maintenance issue, skipped")

    print("\n2) Triaging every open ticket (seeded + guest-detected)")
    # ticket_source="fixtures": the demo is a fixed scenario ("Hotel Aurora,
    # day 0") and must show the same tickets whether or not this property
    # has already filled in its own data/imports/maintenance_tickets.csv -
    # see SIMULATION.md Round 2, finding B. Real passes (tools/run.py) still
    # default to "auto" (CSV import first, fixtures fallback).
    result = triage.compute(settings, store, ticket_source="fixtures")
    for line in result.thinking_log:
        print(f"  {line}")
    triage_item, triage_new = triage.build_triage_run(settings, store, result, provider="mock")
    if triage_new:
        stats["processed"] += 1
        stats["drafted"] += 1
        if triage_item.review_status == "needs_human":
            stats["needs_human"] += 1
    print(f"  triage run {triage_item.id}: status {triage_item.review_status}")

    print("\n3) Building today's cleaning routes")
    high_priority = triage.high_priority_tickets(result)
    # room_source="fixtures" for the same reason as ticket_source above -
    # never the hotel's own data/imports/room_status.csv.
    route_item, route_new = routes.build_route_plan(
        settings, store, day_offset=0, high_priority_tickets=high_priority, provider="mock",
        room_source="fixtures")
    if route_new:
        stats["processed"] += 1
        stats["drafted"] += 1
        if route_item.review_status == "needs_human":
            stats["needs_human"] += 1
    for line in (route_item.draft or {}).get("plan", {}).get("thinking_log", []):
        print(f"  {line}")
    print(f"  route plan {route_item.id}: status {route_item.review_status}")

    print("\n4) The Locksmith: issue a contractor code, then revoke it")
    store_ext.record_lock_event(store, room="Sauna Plant", kind="issue",
                                detail="Contractor sauna seal repair code")
    store_ext.record_lock_event(store, room="Sauna Plant", kind="revoke",
                                detail="Repair finished")
    feed = store_ext.lock_feed(store, limit=2)
    for e in reversed(feed):
        print(f"  {e['kind']:<7} room {e['room']} - {e['detail']}")

    print(f"\n{stats['needs_human']} item(s) need a person to look first before anything "
         "is dispatched or applied (see docs/safety.md).")
    print("Nothing was sent: mode is shadow, and demo never calls "
         "`tools/review.py send` at all.")
    print("Next: `make review` to see what is waiting, or read workflows/10-routes.md "
         "and workflows/11-triage.md.\n")

    print(f"DEMO OK — {summary_line(stats, settings.mode)}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
