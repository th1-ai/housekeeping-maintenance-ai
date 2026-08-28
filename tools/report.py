#!/usr/bin/env python3
"""tools/report.py - what The Steward did, and what it cost.

    make report
    python3 tools/report.py [--since 2026-09-01]

Reads straight from core.store: route plans and triage runs by status, the
ticket book by priority/trade/status, contractor sign-off holds, and LLM
spend (core.store.Store.usage_totals - zero for the mock/interactive
providers). See docs/benefits.md for what each number is meant to show and
docs/how-it-works.md for how it is computed.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", default=None, help="ISO date/time; spend only")
    args = parser.parse_args(argv)

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    store = Store(settings)
    store_ext.ensure_schema(store)
    try:
        print(f"The Steward - report ({settings.hotel.name}, mode {settings.mode})\n")

        counts = store.counts()
        route_plans = store.list_items(kind="route_plan", limit=1000)
        triage_runs = store.list_items(kind="triage_run", limit=1000)
        print(f"Route plans queued: {len(route_plans)}")
        print(f"Triage runs queued: {len(triage_runs)}")
        print(f"Review queue by status: "
             f"{', '.join(f'{k}={v}' for k, v in sorted(counts.items())) or '(empty)'}\n")

        tickets = store_ext.list_tickets(store)
        by_status: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        by_trade: dict[str, int] = {}
        for t in tickets:
            by_status[t.status] = by_status.get(t.status, 0) + 1
            by_priority[t.priority] = by_priority.get(t.priority, 0) + 1
            if t.trade:
                by_trade[t.trade] = by_trade.get(t.trade, 0) + 1
        print(f"Tickets: {len(tickets)} total")
        print(f"  by status:   {', '.join(f'{k}={v}' for k, v in sorted(by_status.items())) or '(none)'}")
        print(f"  by priority: {', '.join(f'{k}={v}' for k, v in sorted(by_priority.items())) or '(none)'}")
        if by_trade:
            print(f"  by trade:    {', '.join(f'{k}={v}' for k, v in sorted(by_trade.items()))}")

        held = [t for t in tickets if t.ai_triage.get("held_for_signoff")]
        if held:
            print(f"\nHeld for chief engineer sign-off: {len(held)} "
                 f"({', '.join(t.id for t in held)})")

        usage = store.usage_totals(since=args.since)
        print(f"\nLLM calls: {usage['calls']}, tokens in/out: "
             f"{usage['input_tokens']}/{usage['output_tokens']}, "
             f"spend: {settings.hotel.currency} {usage['cost_usd']:.4f}")
        if settings.llm.provider in ("mock", "interactive"):
            print(f"(provider is '{settings.llm.provider}' - spend is always zero)")
        return 0
    except StoreError as exc:
        print(f"cannot do that: {exc}", file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
