#!/usr/bin/env python3
"""tools/run.py - The Steward's main loop.

    python3 tools/run.py --once
    python3 tools/run.py --watch
    python3 tools/run.py --once --dry-run
    python3 tools/run.py --once --provider mock

One pass: (1) check unread guest email for a maintenance issue and log a
ticket for anything confident (tools/ticket_intake.py), (2) triage every open
ticket (tools/triage.py) - deterministic trade routing, escalation, the
contractor sign-off gate, scheduling - and queue the run for review, (3)
build today's cleaning routes (tools/routes.py), interleaving any
high-priority ticket into the route that owns its room. Nothing is exported,
posted to the team, or written back to a ticket's final assignee until a
human approves - see workflows/80-review.md.

With `llm.provider: interactive`, every unread email in this pass is checked
before the pass stops - if more than one needs an answer, all of their
prompts are parked in `data/pending/` together, so you answer them all and
re-run once, per CLAUDE.md.

Exit codes: 0 ok, 3 waiting on an `interactive` answer (see the message),
1 a real error.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.adapters import get_email  # noqa: E402
from core.adapters.base import AdapterError  # noqa: E402
from core.config import ConfigError, load_settings  # noqa: E402
from core.llm import LLMError, LLMPendingInteractive  # noqa: E402
from core.log import Run, get_logger, summary_line  # noqa: E402
from core.store import Store  # noqa: E402

import routes  # noqa: E402
import store_ext  # noqa: E402
import ticket_intake  # noqa: E402
import triage  # noqa: E402

log = get_logger("run")


def one_pass(settings, store, *, limit: int, provider: str | None) -> tuple[int, dict]:
    stats = {"processed": 0, "drafted": 0, "needs_human": 0, "sent": 0, "tickets_logged": 0}
    with Run("main", settings, store) as run:
        email = get_email(settings)
        messages = email.fetch_unread(limit=limit)
        # No bulk pre-filter here on purpose: tools/ticket_intake.py's own
        # `if item.intent: return item, False, None` is the single source of
        # truth for "already handled". A bulk `already_processed()` check
        # would also skip a message whose row exists but whose intent is
        # still unset - exactly the case for a message parked on an
        # unanswered `interactive` prompt, which would then never be
        # retried. See workflows/99-troubleshooting.md.
        #
        # A pending prompt on one email must not stop the rest of the batch
        # from being checked too: `_pending_id()` (core/llm.py) keys each
        # prompt on the email's own id, so several can sit in data/pending/
        # at once without colliding. Collect them all, keep going, and only
        # stop the pass once every message in this batch has been tried -
        # that is what lets CLAUDE.md's "answer them all and re-run once"
        # actually hold for a batch of unread guest email.
        pending: list[LLMPendingInteractive] = []
        for msg in messages:
            try:
                item, did_work, ticket = ticket_intake.process_email(
                    settings, store, msg, provider=provider)
            except LLMPendingInteractive as exc:
                pending.append(exc)
                continue
            if not did_work:
                continue
            if ticket is not None:
                stats["tickets_logged"] += 1
                if not settings.dry_run:
                    log.info("ticket logged", ticket_id=ticket.id, room=ticket.room,
                             source_email=msg.id)
            elif item.review_status == "needs_human":
                stats["needs_human"] += 1

        if pending:
            run.stats = dict(stats)
            print(f"{len(pending)} prompt(s) waiting for your answer:\n")
            for exc in pending:
                print(str(exc))
                print()
            print("Answer them all (write each data/pending/*.answer.json), then "
                 "run this command again.")
            return 3, stats

        try:
            triage_result = triage.compute(settings, store)
            triage_item, triage_new = triage.build_triage_run(
                settings, store, triage_result, provider=provider)
        except LLMPendingInteractive as exc:
            run.stats = dict(stats)
            print(str(exc))
            return 3, stats
        if triage_new:
            stats["processed"] += 1
            stats["drafted"] += 1
            if triage_item.review_status == "needs_human":
                stats["needs_human"] += 1
            if not settings.dry_run:
                log.info("triage run queued", item_id=triage_item.id,
                         status=triage_item.review_status, tickets=len(triage_result.decisions))

        high_priority = triage.high_priority_tickets(triage_result)
        try:
            route_item, route_new = routes.build_route_plan(
                settings, store, day_offset=0, high_priority_tickets=high_priority,
                provider=provider)
        except LLMPendingInteractive as exc:
            run.stats = dict(stats)
            print(str(exc))
            return 3, stats
        if route_new:
            stats["processed"] += 1
            stats["drafted"] += 1
            if route_item.review_status == "needs_human":
                stats["needs_human"] += 1
            if not settings.dry_run:
                log.info("route plan queued", item_id=route_item.id,
                         status=route_item.review_status)

        reaped = store.reap_stuck_sending()
        if reaped and not settings.dry_run:
            log.warn("reaped stuck sends", count=len(reaped))
        run.stats = dict(stats)
    return 0, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--once", action="store_true", help="run a single pass (default)")
    mode_group.add_argument("--watch", action="store_true",
                            help="keep running on the configured interval")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute everything, write nothing, even in live mode")
    parser.add_argument("--limit", type=int, default=20, help="max new emails per pass")
    parser.add_argument("--provider", default=None,
                        help="override llm.provider for this run")
    parser.add_argument("--poll-seconds", type=int, default=None,
                        help="override the --watch interval (default: agent.yaml or 1800)")
    args = parser.parse_args(argv)

    try:
        settings = load_settings(provider=args.provider, dry_run=args.dry_run)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    # --dry-run is a rehearsal: compute everything, write nothing - not even
    # to this repo's own data/agent.db, and not a single line to
    # data/logs/*.jsonl claiming a ticket was logged or a run was queued (the
    # log.info calls above are all guarded on settings.dry_run for exactly
    # that reason). An ephemeral in-memory database gives every tool call
    # somewhere real to write during the pass (so the code path - including
    # ticket_intake.py, triage.py and routes.py, all of which just use
    # whichever Store they are handed - is exercised exactly as normal)
    # while guaranteeing nothing lands on disk and nothing from one
    # --dry-run pass can collide with the next one (no rows, no
    # IntegrityError, ever - each pass starts from empty). See
    # factory/workflows/build-repo.md section 5.
    store = Store(settings, path=":memory:" if settings.dry_run else None)
    store_ext.ensure_schema(store)
    try:
        if args.watch:
            poll_seconds = args.poll_seconds or int(settings.agent_get("poll_seconds", 1800))
            while True:
                code, stats = one_pass(settings, store, limit=args.limit,
                                       provider=args.provider)
                print(summary_line(stats, settings.mode))
                if code != 0:
                    return code
                time.sleep(poll_seconds)
        code, stats = one_pass(settings, store, limit=args.limit, provider=args.provider)
        print(summary_line(stats, settings.mode))
        return code
    except AdapterError as exc:
        print(f"integration error: {exc}", file=sys.stderr)
        print("Run `make doctor` to see what is missing and how to fix it.", file=sys.stderr)
        return 1
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
