#!/usr/bin/env python3
"""tools/review.py - work the review queue: list / show / approve / edit / reject / send.

    python3 tools/review.py list [--status pending_review] [--kind route_plan]
    python3 tools/review.py show <id>
    python3 tools/review.py approve <id> [--note "..."]
    python3 tools/review.py edit <id> --note-file note.txt [--headline "..."]
    python3 tools/review.py reject <id> --reason "..."
    python3 tools/review.py retry <id>          # re-queue a failed dispatch/apply
    python3 tools/review.py send                # dispatch/apply everything approved or edited
    python3 tools/review.py stale               # go-live step: clear the shadow-era queue

Two kinds of item live in this queue: ``route_plan`` (today's cleaning
routes) and ``triage_run`` (today's ticket triage). Only this tool writes
`approved` / `edited` / `rejected` (core/review.py). `send` claims whatever
is approved or edited and hands each item to the dispatcher for its own
kind - `tools/routes.py:dispatch_route_plan` or
`tools/triage.py:apply_triage_run` - so one command works for both.
`edit` only rewrites the narrative note; the plan and the trade/schedule
decisions underneath it are deterministic and are not meant to be hand-edited
(docs/how-it-works.md explains why).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, load_settings  # noqa: E402
from core.review import (WriteBlocked, approve, edit, list_queue, reject, retry,  # noqa: E402
                         show, stale_backlog)
from core.store import Store, StoreError  # noqa: E402

import routes  # noqa: E402
import store_ext  # noqa: E402
import triage  # noqa: E402

DISPATCHERS = {
    "route_plan": routes.dispatch_route_plan,
    "triage_run": triage.apply_triage_run,
}


def _print_item_line(item) -> None:
    payload = item.payload or {}
    label = payload.get("date", "") + (f" day{payload['day_offset']}" if "day_offset" in payload else "")
    # `item.is_sample` is set by core (core/store.py) for anything read
    # through a mock adapter outside `make demo` - see docs/integrations.md
    # "Sample data is labelled".
    marker = "  [SAMPLE DATA]" if item.is_sample else ""
    print(f"  {item.id}  {item.review_status:<14} {item.kind:<12} {label}{marker}")


def cmd_list(store, args) -> int:
    items = list_queue(store, status=args.status, kind=args.kind, limit=args.limit)
    if not items:
        print("Nothing is waiting for you.")
        return 0
    print(f"{len(items)} item(s) waiting:\n")
    for item in items:
        _print_item_line(item)
    print("\nRun `python3 tools/review.py show <id>` for the full plan.")
    return 0


def cmd_show(store, args) -> int:
    try:
        detail = show(store, args.id)
    except KeyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if (detail["item"].get("payload") or {}).get("_sample"):
        print("[SAMPLE DATA] this item was read through a mock adapter, not your "
             "property - see docs/integrations.md.\n")
    print(json.dumps(detail, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_approve(store, args) -> int:
    item = approve(store, args.id, note=args.note or "")
    print(f"approved {item.id} - now in the send queue")
    return 0


def cmd_edit(store, args) -> int:
    item = store.get_item(args.id)
    if item is None:
        print(f"error: no item {args.id}", file=sys.stderr)
        return 1
    note_text = Path(args.note_file).read_text(encoding="utf-8").strip()
    new_draft = dict(item.draft or {})
    narrative = dict(new_draft.get("narrative") or {})
    narrative["note"] = note_text
    if args.headline:
        narrative["headline"] = args.headline
    new_draft["narrative"] = narrative
    edit(store, args.id, new_draft, note=args.note or "")
    print(f"edited {item.id} - now in the send queue")
    return 0


def cmd_reject(store, args) -> int:
    item = reject(store, args.id, reason=args.reason or "")
    print(f"rejected {item.id}")
    return 0


def cmd_retry(store, args) -> int:
    item = retry(store, args.id)
    print(f"queued {item.id} for another dispatch/apply attempt")
    return 0


def cmd_send(store, settings, args) -> int:
    claimed = store.claim_for_send(limit=args.limit)
    if not claimed:
        print("Nothing approved or edited is waiting to send.")
        return 0
    sent, failed = 0, 0
    for item in claimed:
        dispatcher = DISPATCHERS.get(item.kind)
        if dispatcher is None:
            store.mark_send_failed(item.id, f"no dispatcher for kind '{item.kind}'")
            print(f"failed {item.id}: no dispatcher for kind '{item.kind}'")
            failed += 1
            continue
        try:
            result = dispatcher(settings, store, item)
        except WriteBlocked as exc:
            # Not a failure: the mode blocked it. The approval stands for
            # go-live - `tools/review.py stale` is what clears a shadow-era
            # approval, never a blocked send.
            store.transition(item.id, "approved", "agent", {"blocked": str(exc)[:200]})
            print(f"blocked {item.id} (approval kept): {exc}")
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - record and move on, never crash the batch
            store.mark_send_failed(item.id, str(exc))
            print(f"failed {item.id}: {exc}")
            failed += 1
            continue
        store.mark_sent(item.id, result.get("message_id"))
        print(f"sent {item.id} ({item.kind}): {result}")
        sent += 1
    print(f"\n{sent} sent, {failed} failed.")
    return 0 if failed == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="what is waiting for a human")
    p_list.add_argument("--status", default=None)
    p_list.add_argument("--kind", default=None, help="route_plan | triage_run")
    p_list.add_argument("--limit", type=int, default=50)

    p_show = sub.add_parser("show", help="full detail for one item")
    p_show.add_argument("id")

    p_approve = sub.add_parser("approve", help="approve the plan unchanged")
    p_approve.add_argument("id")
    p_approve.add_argument("--note", default="")

    p_edit = sub.add_parser("edit", help="rewrite the narrative note, then queue it")
    p_edit.add_argument("id")
    p_edit.add_argument("--note-file", required=True)
    p_edit.add_argument("--headline", default=None)
    p_edit.add_argument("--note", default="")

    p_reject = sub.add_parser("reject", help="discard the plan")
    p_reject.add_argument("id")
    p_reject.add_argument("--reason", default="")

    p_retry = sub.add_parser("retry", help="re-queue a failed dispatch/apply")
    p_retry.add_argument("id")

    p_send = sub.add_parser("send", help="dispatch/apply everything approved or edited")
    p_send.add_argument("--limit", type=int, default=20)

    sub.add_parser("stale", help="go-live step: mark everything still un-sent as stale "
                                 "(the shadow-era queue was never dispatched/applied and "
                                 "is out of date)")

    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    store_ext.ensure_schema(store)
    try:
        if args.command == "list":
            return cmd_list(store, args)
        if args.command == "show":
            return cmd_show(store, args)
        if args.command == "approve":
            return cmd_approve(store, args)
        if args.command == "edit":
            return cmd_edit(store, args)
        if args.command == "reject":
            return cmd_reject(store, args)
        if args.command == "retry":
            return cmd_retry(store, args)
        if args.command == "send":
            return cmd_send(store, settings, args)
        if args.command == "stale":
            moved = stale_backlog(store)
            print(f"marked {len(moved)} item(s) stale. Nothing from before go-live "
                 "will be dispatched or applied.")
            return 0
        parser.error(f"unknown command {args.command}")
        return 2
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
