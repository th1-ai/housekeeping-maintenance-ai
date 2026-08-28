#!/usr/bin/env python3
"""tools/routes.py - I/O around the route optimiser engine (tools/engine.py).

    python3 tools/routes.py optimise [--day-offset 0] [--provider mock]

Reads today's room board (`store_ext.load_room_board`), asks
`tools/triage.py:compute()` which open tickets are high priority right now,
and calls `engine.optimise_routes()` - both the wing and the flat layouts are
costed on every run so the walking-time saving is provable, not asserted (see
docs/how-it-works.md). The result plus a short narrative
(`prompts/route-note.md`) is queued as one review item; nothing is exported
or posted to the team until a human approves it (`tools/review.py`).
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

from core.adapters import get_messaging, get_sheets  # noqa: E402
from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.llm import LLMResult, complete  # noqa: E402
from core.store import Item, Store, StoreError  # noqa: E402
from core.templates import build_prompt  # noqa: E402

import engine  # noqa: E402
import store_ext  # noqa: E402
import triage  # noqa: E402

SCHEMAS_DIR = REPO_ROOT / "prompts" / "schemas"
ROUTE_NOTE_SCHEMA = json.loads((SCHEMAS_DIR / "route-note.json").read_text(encoding="utf-8"))


def _rooms(room_source: str = "auto") -> list[engine.Room]:
    """``room_source`` is passed straight through to ``store_ext.load_room_board``
    - ``"auto"`` (default) for a real pass, ``"fixtures"`` for ``tools/demo.py``
    so the demo never reads a hotel's own ``data/imports/room_status.csv``.
    """
    return [engine.Room(room_number=str(r["room_number"]), floor=int(r["floor"]),
                        room_type=r.get("room_type", ""), status=r.get("status", "vacant"),
                        vip=bool(r.get("vip", False)), note=r.get("note", ""))
           for r in store_ext.load_room_board(source=room_source)]


def _rules(settings: Settings) -> dict:
    return dict(settings.agent_get("rules", {}) or {})


def _card_to_dict(card: engine.RouteCard) -> dict:
    return {
        "attendant": card.attendant, "floors": card.floors, "room_count": card.room_count,
        "service_minutes": card.service_minutes, "walking_minutes": card.walking_minutes,
        "counts": card.counts, "first_rooms": card.first_rooms, "all_rooms": card.all_rooms,
    }


def _plan_to_dict(plan: engine.RoutePlan) -> dict:
    return {
        "day_offset": plan.day_offset, "total_rooms": plan.total_rooms,
        "in_play_count": plan.in_play_count, "counts": plan.counts,
        "routes": [_card_to_dict(c) for c in plan.routes], "strategy_used": plan.strategy_used,
        "wing_walking_minutes": plan.wing_walking_minutes,
        "flat_walking_minutes": plan.flat_walking_minutes,
        "minutes_saved": plan.minutes_saved, "percent_saved": plan.percent_saved,
        "cleaning_hours": plan.cleaning_hours, "vip_flags": plan.vip_flags,
        "maintenance_slots": [vars(m) for m in plan.maintenance_slots],
        "capacity_warning": plan.capacity_warning, "thinking_log": plan.thinking_log,
    }


def build_route_plan(settings: Settings, store: Store, *, day_offset: int = 0,
                     high_priority_tickets: list[engine.Ticket] | None = None,
                     provider: str | None = None, room_source: str = "auto") -> tuple[Item, bool]:
    """Compute today's route plan and queue it for review.

    Idempotent per calendar day + day_offset: a second call the same day
    returns the existing item untouched. See ``_rooms`` for ``room_source``.
    """
    today = date.today().isoformat()
    external_id = f"{today}-day{day_offset}"
    item = store.upsert_item("housekeeping-routes", external_id, kind="route_plan",
                             payload={"day_offset": day_offset, "date": today})
    if item.intent:
        return item, False

    plan = engine.optimise_routes(_rooms(room_source=room_source), high_priority_tickets or [],
                                  rules=_rules(settings), config=settings.agent,
                                  day_offset=day_offset)
    plan_dict = _plan_to_dict(plan)

    prompt = build_prompt("route-note", settings=settings, item=plan_dict)
    llm_result: LLMResult = complete("route-note", prompt, ROUTE_NOTE_SCHEMA,
                                     settings=settings, provider=provider, store=store,
                                     item_id=item.id, fixture_id=f"routes-{day_offset}")
    narrative = llm_result.data or {}
    store.set_fields(item.id, intent=plan.strategy_used,
                     draft={"plan": plan_dict, "narrative": narrative})
    needs_human = bool(plan.capacity_warning) or any(
        m["kind"] == "unscheduled" for m in plan_dict["maintenance_slots"])
    status = "needs_human" if needs_human else "pending_review"
    updated = store.transition(item.id, status, actor="agent",
                               detail={"routes": len(plan.routes)})
    return updated, True


def dispatch_route_plan(settings: Settings, store: Store, item: Item) -> dict:
    """Called by ``tools/review.py send`` once the plan is approved/edited.

    "Dispatch to the housekeeping app" in this repo means: export the route
    cards to a sheet every attendant can open, and post the morning note to
    the staff channel. Neither is a specific app integration - see
    docs/how-it-works.md "Design decisions".
    """
    plan = (item.draft or {}).get("plan", {})
    sheets = get_sheets(settings)
    rows = [["attendant", "floors", "rooms", "service_minutes", "walking_minutes", "first_rooms"]]
    for card in plan.get("routes", []):
        rows.append([card["attendant"], "/".join(str(f) for f in card["floors"]),
                    card["room_count"], card["service_minutes"], card["walking_minutes"],
                    ", ".join(card["first_rooms"])])
    sheet_name = f"routes-{item.payload.get('date', '')}"
    sheets.write(sheet_name, rows, item=item)

    messaging = get_messaging(settings)
    note = (item.draft or {}).get("narrative", {}).get("note", "")
    if note:
        messaging.notify_staff(f"Housekeeping plan: {note}", item=item)
    return {"message_id": sheet_name}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def cmd_optimise(store: Store, settings: Settings, args: argparse.Namespace) -> int:
    result = triage.compute(settings, store)
    high_priority = triage.high_priority_tickets(result)
    item, did_work = build_route_plan(settings, store, day_offset=args.day_offset,
                                      high_priority_tickets=high_priority,
                                      provider=args.provider)
    if not did_work:
        print(f"Today's route plan was already computed: {item.id} ({item.review_status}).")
        return 0
    for line in (item.draft or {}).get("plan", {}).get("thinking_log", []):
        print(f"  {line}")
    print(f"\nRoute plan {item.id}: status {item.review_status}.")
    print("Run `make review` to see it, then `python3 tools/review.py approve "
         f"{item.id}` and `python3 tools/review.py send` to dispatch it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_opt = sub.add_parser("optimise", help="build (or show) today's route plan")
    p_opt.add_argument("--day-offset", type=int, default=0)
    p_opt.add_argument("--provider", default=None)

    args = parser.parse_args(argv)
    try:
        settings = load_settings(provider=args.provider if hasattr(args, "provider") else None)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    store_ext.ensure_schema(store)
    try:
        if args.command == "optimise":
            return cmd_optimise(store, settings, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
