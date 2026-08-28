#!/usr/bin/env python3
"""tools/locks.py - The Locksmith: the key-issue audit feed.

    python3 tools/locks.py issue --room 214 --detail "Contractor sauna repair code"
    python3 tools/locks.py revoke --room 214 --detail "Repair finished"
    python3 tools/locks.py feed [--limit 60]

The Locksmith has no roster card of its own - it is a built-in feature of
this agent, not a separately-branded sub-agent, so it has no
`workflows/2x-*.md` file; see workflows/12-locks.md and docs/sub-agents.md.

Every issue/revoke is a direct action a person runs deliberately, the same
trust level as clicking the button in the demo - there is no draft to review
first. What this repo does NOT do is call a real electronic lock system: it
only appends to this agent's own `hk_lock_events` table, matching the source
spec exactly ("no real lock/PMS-key-system call"). Connecting a real system
means implementing `core/adapters/base.py:Locks.issue_key()` - see
docs/integrations.md#implement-your-own.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, load_settings  # noqa: E402
from core.store import Store, StoreError  # noqa: E402

import store_ext  # noqa: E402


def cmd_issue(store: Store, args: argparse.Namespace) -> int:
    row_id = store_ext.record_lock_event(store, room=args.room, kind="issue",
                                         detail=args.detail, actor=args.actor)
    print(f"issued: room {args.room} - {args.detail or '(no detail)'} (event {row_id})")
    print("A simulated code card would render here in a real front desk app - "
         "no real lock system was called. See docs/integrations.md.")
    return 0


def cmd_revoke(store: Store, args: argparse.Namespace) -> int:
    row_id = store_ext.record_lock_event(store, room=args.room, kind="revoke",
                                         detail=args.detail, actor=args.actor)
    print(f"revoked: room {args.room} - {args.detail or '(no detail)'} (event {row_id})")
    return 0


def cmd_feed(store: Store, args: argparse.Namespace) -> int:
    events = store_ext.lock_feed(store, limit=args.limit)
    if not events:
        print("No lock events yet.")
        return 0
    for e in events:
        print(f"  {e['created_at']}  {e['kind']:<7} {e['room']:<16} {e['actor']:<14} "
             f"{e['detail'] or ''}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_issue = sub.add_parser("issue", help="issue a key/code for a room")
    p_issue.add_argument("--room", required=True)
    p_issue.add_argument("--detail", default="")
    p_issue.add_argument("--actor", default="The Locksmith")

    p_revoke = sub.add_parser("revoke", help="revoke a previously issued key/code")
    p_revoke.add_argument("--room", required=True)
    p_revoke.add_argument("--detail", default="")
    p_revoke.add_argument("--actor", default="The Locksmith")

    p_feed = sub.add_parser("feed", help="show the audit feed, newest first")
    p_feed.add_argument("--limit", type=int, default=60)

    args = parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    store_ext.ensure_schema(store)
    try:
        if args.command == "issue":
            return cmd_issue(store, args)
        if args.command == "revoke":
            return cmd_revoke(store, args)
        if args.command == "feed":
            return cmd_feed(store, args)
        parser.error(f"unknown command {args.command}")
        return 2
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
