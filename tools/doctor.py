#!/usr/bin/env python3
"""tools/doctor.py - is The Steward configured and reachable right now?

    make doctor
    python3 tools/doctor.py

Runs the generic core.doctor checks (python, deps, config, .env, hotel
identity, mode, llm provider, every adapter, the store, knowledge) plus
Steward-specific ones: the five hk_rules toggles, the two named engineers,
the prompt files, and the room board / ticket fixtures. Exits 0 when
everything passed, 1 when a FAIL line needs fixing. Never a traceback.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import ConfigError, Settings, load_settings  # noqa: E402
from core.doctor import Check, FAIL, PASS, WARN, print_table, run_checks  # noqa: E402

import store_ext  # noqa: E402


def check_rules(settings: Settings) -> Check:
    rules = settings.agent_get("rules", {}) or {}
    expected = {"wing_routing", "checkout_first", "deep_clean_cadence",
               "maintenance_interleave", "contractor_threshold"}
    missing = sorted(expected - set(rules))
    if missing:
        return Check("hk rules", FAIL, f"missing toggle(s): {', '.join(missing)}",
                     "Copy config/agent.example.yaml to config/agent.yaml - it ships "
                     "with all five.")
    on = sorted(k for k, v in rules.items() if v)
    return Check("hk rules", PASS, f"{len(rules)} configured, on: {', '.join(on) or 'none'}")


def check_engineers(settings: Settings) -> Check:
    engineers = settings.agent_get("engineers", {}) or {}
    if not engineers.get("mechanical") or not engineers.get("electrical"):
        return Check("engineers", FAIL, "mechanical/electrical engineer names not set",
                     "Set config/agent.yaml: engineers.mechanical / engineers.electrical.")
    return Check("engineers", PASS,
                 f"mechanical={engineers['mechanical']}, electrical={engineers['electrical']}")


def check_prompts() -> Check:
    missing = [p for p in ("prompts/route-note.md", "prompts/triage-note.md",
                           "prompts/ticket-detect.md", "prompts/schemas/route-note.json",
                           "prompts/schemas/triage-note.json",
                           "prompts/schemas/ticket-detect.json")
              if not (REPO_ROOT / p).is_file()]
    if missing:
        return Check("prompts", FAIL, f"missing {', '.join(missing)}",
                     "These ship with the repo - restore them from git.")
    return Check("prompts", PASS, "route-note / triage-note / ticket-detect + schemas present")


def check_board_data() -> Check:
    rooms = store_ext.load_room_board()
    tickets = store_ext.load_seed_tickets()
    if not rooms:
        return Check("room board", FAIL, "no rooms in data/imports/room_status.csv or "
                     "fixtures/hotel/room_status.json",
                     "See docs/integrations.md for the CSV columns, or restore the fixture.")
    detail = f"{len(rooms)} rooms, {len(tickets)} seeded ticket(s)"
    if not tickets:
        return Check("room board", WARN, detail, "No maintenance tickets to triage yet - "
                     "that is fine on a brand-new property.")
    return Check("room board", PASS, detail)


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        checks = run_checks(None) + [Check("config", FAIL, str(exc),
                                           "Fix config/hotel.yaml or config/agent.yaml.")]
        return print_table(checks, title="The Steward - doctor")

    checks = run_checks(settings, extra=[check_rules, check_engineers])
    checks.append(check_prompts())
    checks.append(check_board_data())
    return print_table(checks, title="The Steward - doctor")


if __name__ == "__main__":
    raise SystemExit(main())
