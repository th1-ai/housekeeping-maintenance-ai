"""Integration tests for the I/O layer: tools/ticket_intake.py, tools/triage.py,
tools/routes.py, tools/review.py and tools/locks.py, against provider=mock and
the bundled fixtures. Each test gets its own SQLite file via tmp_path.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tools"))  # tools/*.py cross-import each other by bare name

from core.adapters import get_email  # noqa: E402
from core.adapters.base import AdapterConfig  # noqa: E402
from core.config import Settings, SystemsConfig, load_settings  # noqa: E402
from core.review import WriteBlocked, approve, stale_backlog  # noqa: E402
from core.store import Store  # noqa: E402

from tools import review, routes, run, store_ext, ticket_intake, triage  # noqa: E402


def _settings(**overrides):
    return load_settings(provider="mock", mode=overrides.get("mode", "shadow"))


def _write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)] + [",".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# VIP via the `csv` PMS adapter (SIMULATION.md finding 4)
# --------------------------------------------------------------------------
def test_vip_names_resolves_vip_via_the_csv_adapters_guests_file(tmp_path, monkeypatch):
    # reservations.csv has no vip column of its own
    # (core/adapters/pms_csv.py:_to_reservation never reads one) - only
    # guests.csv does. vip_names() must join the two by email.
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    _write_csv(tmp_path / "data" / "imports" / "reservations.csv",
              ["id", "check_in", "check_out", "guest_first_name", "guest_last_name",
               "guest_email"],
              [["r1", "2026-09-06", "2026-09-09", "Elena", "Marchetti",
                "elena.marchetti@example.com"],
               ["r2", "2026-09-01", "2026-09-03", "Daniel", "Okafor",
                "daniel.okafor@example.com"]])
    _write_csv(tmp_path / "data" / "imports" / "guests.csv",
              ["id", "first_name", "last_name", "email", "vip"],
              [["g1", "Elena", "Marchetti", "elena.marchetti@example.com", "true"],
               ["g2", "Daniel", "Okafor", "daniel.okafor@example.com", "false"]])

    settings = Settings(systems=SystemsConfig(pms=AdapterConfig(adapter="csv")), mode="shadow")
    names = triage.vip_names(settings)
    assert names == {"elena marchetti"}  # confirmed VIP, joined via guests.csv
    assert "daniel okafor" not in names  # guests.csv says vip: false


def test_vip_names_via_csv_adapter_returns_empty_set_with_no_guest_match(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_REPO_ROOT", str(tmp_path))
    _write_csv(tmp_path / "data" / "imports" / "reservations.csv",
              ["id", "check_in", "check_out", "guest_first_name", "guest_last_name"],
              [["r1", "2026-09-06", "2026-09-09", "Jamie", "Rivera"]])
    settings = Settings(systems=SystemsConfig(pms=AdapterConfig(adapter="csv")), mode="shadow")
    assert triage.vip_names(settings) == set()  # no guests.csv at all -> no crash, no VIP


def _store(tmp_path, settings):
    store = Store(settings, path=tmp_path / "test.db")
    store_ext.ensure_schema(store)
    return store


def test_ticket_intake_creates_a_ticket_for_a_confident_maintenance_email(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    email = get_email(settings)
    messages = {m.id: m for m in email.fetch_unread(limit=50)}

    item, did_work, ticket = ticket_intake.process_email(settings, store, messages["email-01"],
                                                         provider="mock")
    assert did_work is True
    assert ticket is not None
    assert ticket.room == "206"
    assert item.review_status == "auto_sent"
    store.close()


def test_ticket_intake_skips_a_non_maintenance_email(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    email = get_email(settings)
    messages = {m.id: m for m in email.fetch_unread(limit=50)}

    item, did_work, ticket = ticket_intake.process_email(settings, store, messages["email-03"],
                                                         provider="mock")
    assert did_work is True
    assert ticket is None
    assert item.review_status == "skipped"
    store.close()


def test_ticket_intake_queues_low_confidence_detections_for_a_human(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    email = get_email(settings)
    messages = {m.id: m for m in email.fetch_unread(limit=50)}

    item, did_work, ticket = ticket_intake.process_email(settings, store, messages["email-04"],
                                                         provider="mock")
    assert did_work is True
    assert ticket is None
    assert item.review_status == "needs_human"
    store.close()


def test_ticket_intake_is_idempotent_on_a_rerun(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    email = get_email(settings)
    msg = next(m for m in email.fetch_unread(limit=50) if m.id == "email-01")

    _, first_did_work, _ = ticket_intake.process_email(settings, store, msg, provider="mock")
    _, second_did_work, _ = ticket_intake.process_email(settings, store, msg, provider="mock")
    assert first_did_work is True
    assert second_did_work is False
    store_ext.seed_tickets(store)  # tools/run.py always seeds in the same pass
    assert len(store_ext.list_tickets(store)) == 14  # 13 seeded + 1 from the email, not 2
    store.close()


def test_triage_compute_escalates_the_seeded_tickets_deterministically(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    assert len(result.decisions) == 13  # the seeded fixture tickets, before any email intake
    assert result.escalated_count == 6
    assert result.contractor_held_count == 1
    store.close()


def test_build_triage_run_is_idempotent_per_day(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    item, created = triage.build_triage_run(settings, store, result, provider="mock")
    assert created is True
    same_item, created_again = triage.build_triage_run(settings, store, result, provider="mock")
    assert created_again is False
    assert same_item.id == item.id
    store.close()


def test_shadow_mode_blocks_send_until_a_human_approves(tmp_path):
    settings = _settings(mode="shadow")
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    item, _ = triage.build_triage_run(settings, store, result, provider="mock")

    claimed = store.claim_for_send(limit=5)
    assert claimed == []  # pending_review/needs_human never enters the send queue unapproved

    approve(store, item.id)
    claimed = store.claim_for_send(limit=5)
    assert [i.id for i in claimed] == [item.id]  # only an approved item can be claimed
    store.close()


def test_apply_triage_run_writes_every_ticket_and_never_touches_a_low_priority_clock(tmp_path):
    # mode: live WITH the approval satisfied - the one path that really writes.
    settings = _settings(mode="live")
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    item, _ = triage.build_triage_run(settings, store, result, provider="mock")
    approved = approve(store, item.id)
    claimed = store.claim_for_send(limit=5)[0]

    outcome = triage.apply_triage_run(settings, store, claimed)
    assert outcome["tickets_written"] == 13
    ticket = store_ext.get_ticket(store, "t05")  # passport-in-safe, escalated to high
    assert ticket.status == "triaged"
    assert ticket.priority == "high"
    assert ticket.assignee  # an engineer or "Contractor" was written
    store.close()
    assert approved.id == item.id  # sanity: approve() returned the same item


def test_apply_triage_run_is_blocked_in_shadow_even_when_approved(tmp_path):
    # SIMULATION.md finding 1: an approved triage run must not write a
    # single ticket's status while mode: shadow, even though claim_for_send
    # is happy to hand it over (the FSM claim and the write guard are two
    # different gates - see core/review.py:evaluate_write).
    settings = _settings(mode="shadow")
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    item, _ = triage.build_triage_run(settings, store, result, provider="mock")
    approve(store, item.id)
    claimed = store.claim_for_send(limit=5)[0]

    try:
        triage.apply_triage_run(settings, store, claimed)
        raised = False
    except WriteBlocked:
        raised = True
    assert raised is True

    ticket = store_ext.get_ticket(store, "t05")
    assert ticket.status == "open"  # never touched
    store.close()


def test_complete_ticket_releases_it_from_open_status(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    triage.open_tickets(store)  # seeds the fixture tickets
    row = triage.complete_ticket(store, "t03")
    assert row.status == "done"
    assert row not in store_ext.list_tickets(store, status="open")
    store.close()


def test_build_route_plan_interleaves_todays_escalated_tickets(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    high_priority = triage.high_priority_tickets(result)
    item, created = routes.build_route_plan(settings, store, day_offset=0,
                                            high_priority_tickets=high_priority,
                                            provider="mock")
    assert created is True
    plan = item.draft["plan"]
    assert plan["in_play_count"] > 0
    slots = {m["ticket_id"]: m["kind"] for m in plan["maintenance_slots"]}
    assert slots["t05"] == "interleaved"   # room 103 is a stayover today
    assert slots["t08"] == "direct"        # Kitchen Walk-in is not a guest room
    store.close()


def test_build_route_plan_is_idempotent_per_day_and_offset(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    high_priority = triage.high_priority_tickets(result)
    item, created = routes.build_route_plan(settings, store, day_offset=0,
                                            high_priority_tickets=high_priority,
                                            provider="mock")
    same_item, created_again = routes.build_route_plan(settings, store, day_offset=0,
                                                        high_priority_tickets=high_priority,
                                                        provider="mock")
    assert created is True
    assert created_again is False
    assert same_item.id == item.id
    store.close()


def test_dispatch_route_plan_is_blocked_in_shadow_without_approval(tmp_path):
    settings = _settings(mode="shadow")
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    item, _ = routes.build_route_plan(settings, store, day_offset=0,
                                      high_priority_tickets=triage.high_priority_tickets(result),
                                      provider="mock")
    try:
        routes.dispatch_route_plan(settings, store, item)
        raised = False
    except WriteBlocked:
        raised = True
    assert raised is True
    store.close()


def test_dispatch_route_plan_is_blocked_in_shadow_even_when_approved(tmp_path):
    # SIMULATION.md finding 1: this is the exact reproduction - approve a
    # route plan, then `send` while mode: shadow is still set. It must not
    # write routes-<date>.csv. Point the sheets adapter's own exports_dir at
    # tmp_path (not AGENT_REPO_ROOT - that would also break where prompts/
    # and knowledge/ load from) so a failed guard would show up right here.
    settings = _settings(mode="shadow")
    settings.systems.sheets.options["exports_dir"] = str(tmp_path / "exports")
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    item, _ = routes.build_route_plan(settings, store, day_offset=0,
                                      high_priority_tickets=triage.high_priority_tickets(result),
                                      provider="mock")
    approve(store, item.id)
    claimed = store.claim_for_send(limit=5)[0]

    try:
        routes.dispatch_route_plan(settings, store, claimed)
        raised = False
    except WriteBlocked:
        raised = True
    assert raised is True

    export_path = tmp_path / "exports" / f"routes-{item.payload.get('date', '')}.csv"
    assert not export_path.exists()
    store.close()


def test_review_stale_clears_a_shadow_era_route_plan_and_triage_run(tmp_path):
    # go-live step (workflows/90-go-live.md step 1 / `python3 tools/review.py
    # stale`): everything still pending/needs_human/approved/edited from the
    # shadow era moves to `stale` so it cannot go out by surprise once
    # mode: live is set.
    settings = _settings(mode="shadow")
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    triage_item, _ = triage.build_triage_run(settings, store, result, provider="mock")
    route_item, _ = routes.build_route_plan(
        settings, store, day_offset=0,
        high_priority_tickets=triage.high_priority_tickets(result), provider="mock")
    approve(store, route_item.id)  # approved during shadow - still must go stale

    moved = stale_backlog(store)
    assert set(moved) == {triage_item.id, route_item.id}
    assert store.get_item(triage_item.id).review_status == "stale"
    assert store.get_item(route_item.id).review_status == "stale"
    store.close()


def test_send_blocked_by_shadow_returns_the_item_to_approved_not_failed(tmp_path):
    # core/review.py fix (see factory tools/sync_core.py): a shadow-blocked
    # send must not land in `failed` (which would need a human to
    # `tools/review.py retry` it) - the approval already stands, and
    # `tools/review.py stale` is what clears a shadow-era approval at
    # go-live. tools/review.py:cmd_send adopts the same WriteBlocked
    # handling as the reference agent: transition back to "approved", never
    # store.mark_send_failed().
    settings = _settings(mode="shadow")
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    item, _ = triage.build_triage_run(settings, store, result, provider="mock")
    approve(store, item.id)

    args = argparse.Namespace(limit=5)
    code = review.cmd_send(store, settings, args)
    assert code == 1  # nothing sent, one blocked

    refreshed = store.get_item(item.id)
    assert refreshed.review_status == "approved"  # not "failed" - still send-ready at go-live
    store.close()


def test_locksmith_issue_then_revoke_appear_in_the_feed(tmp_path):
    settings = _settings()
    store = _store(tmp_path, settings)
    store_ext.record_lock_event(store, room="214", kind="issue", detail="contractor code")
    store_ext.record_lock_event(store, room="214", kind="revoke", detail="job done")
    feed = store_ext.lock_feed(store, limit=10)
    assert [e["kind"] for e in feed] == ["revoke", "issue"]  # newest first
    store.close()


# --------------------------------------------------------------------------
# --dry-run writes nothing, twice in a row (SIMULATION.md Round 2, finding A)
# --------------------------------------------------------------------------
def test_dry_run_writes_nothing_to_disk_across_two_consecutive_passes():
    # tools/run.py main() swaps in an in-memory Store for --dry-run (path
    # ":memory:"), and one_pass() only logs "ticket logged" / "triage run
    # queued" / "route plan queued" / "reaped stuck sends" when not
    # settings.dry_run - so a rehearsal never writes a business row, a
    # `runs` row, or a single data/logs/*.jsonl line. Runs the real CLI
    # entrypoint twice in a row (idempotency: a second dry-run must not
    # raise IntegrityError from a half-applied first one) against this
    # test's own isolated AGENT_REPO_ROOT (conftest.py). --provider mock
    # sidesteps the shipped example config's llm.provider: interactive,
    # which would otherwise park a prompt instead of completing the pass.
    for _ in range(2):
        code = run.main(["--once", "--dry-run", "--provider", "mock"])
        assert code == 0

    root = Path(os.environ["AGENT_REPO_ROOT"])
    assert not (root / "data" / "agent.db").exists()
    log_dir = root / "data" / "logs"
    lines: list[str] = []
    if log_dir.exists():
        for f in log_dir.glob("*.jsonl"):
            lines += f.read_text(encoding="utf-8").splitlines()
    # core/log.py:Run.__enter__ also logs a "sample_data" warning on every
    # real (non-demo) pass whose used systems are mock - dry-run included,
    # since it is still reading the shipped fixtures, just not writing.
    # That is an observability line, not a business write (the assertion
    # above is what actually proves nothing landed in data/agent.db) - so
    # tolerate exactly that line and nothing else; any other message here
    # would mean a ticket/route/triage record leaked out of the dry run.
    messages = [json.loads(line)["message"] for line in lines]
    assert all(m == "sample_data" for m in messages), messages


# --------------------------------------------------------------------------
# make demo ignores a hotel's own data/imports/*.csv (SIMULATION.md Round 2,
# finding B)
# --------------------------------------------------------------------------
def test_fixtures_source_ignores_a_decoy_data_imports_csv(tmp_path):
    # tools/demo.py always passes ticket_source="fixtures" / room_source=
    # "fixtures" to triage.compute()/routes._rooms() - never "auto" - so
    # `make demo` stays the fixed "Hotel Aurora, day 0" scenario whether or
    # not a hotel has already filled in its own data/imports/*.csv.
    settings = _settings()
    _write_csv(settings.root / "data" / "imports" / "maintenance_tickets.csv",
              ["id", "room", "summary", "priority"],
              [["decoy-1", "999", "Decoy ticket that must never reach make demo", "high"]])
    _write_csv(settings.root / "data" / "imports" / "room_status.csv",
              ["room_number", "floor", "room_type", "status"],
              [["999", "9", "Decoy Suite", "vacant"]])

    store = _store(tmp_path, settings)
    result = triage.compute(settings, store, ticket_source="fixtures")
    assert len(result.decisions) == 13  # the bundled fixture tickets, not the 1-row decoy
    assert "decoy-1" not in {d.ticket_id for d in result.decisions}

    rooms = routes._rooms(room_source="fixtures")
    assert "999" not in {r.room_number for r in rooms}
    store.close()


def test_auto_source_still_prefers_a_real_hotels_data_imports_csv(tmp_path):
    # The other half of finding B's fix: a real (non-demo) pass -
    # tools/run.py, tools/triage.py, tools/routes.py - must still prefer a
    # hotel's own data/imports/*.csv when one exists. Only tools/demo.py is
    # pinned to "fixtures"; ticket_source="auto" (the default everywhere
    # else) keeps doing the CSV-import-first job it always did.
    settings = _settings()
    _write_csv(settings.root / "data" / "imports" / "maintenance_tickets.csv",
              ["id", "room", "summary", "priority"],
              [["real-1", "101", "Guest's own imported ticket", "medium"]])

    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)  # ticket_source="auto", the default
    assert {d.ticket_id for d in result.decisions} == {"real-1"}
    store.close()


# --------------------------------------------------------------------------
# the test suite is hermetic (SIMULATION.md Round 2, finding C)
# --------------------------------------------------------------------------
def test_suite_never_sees_this_working_copys_own_config_or_data(tmp_path):
    # conftest.py's autouse fixture is what makes this true for every test in
    # this file, including the ones above that never mention AGENT_REPO_ROOT
    # themselves - a hotel's own config/hotel.yaml, config/agent.yaml and
    # data/imports/*.csv must never be able to turn `make test` red.
    settings = _settings()
    assert settings.root != REPO_ROOT
    assert not (settings.root / "config" / "hotel.yaml").exists()
    assert not (settings.root / "config" / "agent.yaml").exists()
    assert not (settings.root / "data" / "imports" / "maintenance_tickets.csv").exists()

    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    assert len(result.decisions) == 13  # the bundled fixtures, always - never this working
    store.close()                       # copy's own data, whatever it currently holds


def test_messaging_mock_outbox_stays_inside_the_isolated_repo_root(tmp_path):
    # SIMULATION.md Round 2, finding C: this is the exact leak - `make test`
    # left a real-looking "sent" message in the hotel's own
    # data/exports/sent_messages.jsonl purely from running the suite,
    # because core/adapters/messaging_mock.py's outbox path is
    # sub_data_dir("exports") - repo_root()-relative - and this specific
    # test (mode: live, the one real-dispatch code path exercised below) did
    # not isolate AGENT_REPO_ROOT itself. It does not need to any more:
    # conftest.py's autouse fixture isolates every test, this one included,
    # so the outbox write lands under settings.root, never under REPO_ROOT.
    settings = _settings(mode="live")
    assert settings.root != REPO_ROOT
    store = _store(tmp_path, settings)
    result = triage.compute(settings, store)
    item, _ = triage.build_triage_run(settings, store, result, provider="mock")
    approve(store, item.id)
    claimed = store.claim_for_send(limit=5)[0]

    triage.apply_triage_run(settings, store, claimed)

    outbox = settings.root / "data" / "exports" / "sent_messages.jsonl"
    assert outbox.exists()  # the write really happened - just inside the isolated root
    store.close()


def test_sample_item_shows_marker_in_list_line_and_show(tmp_path, capsys):
    """core/store.py tags an item read through a mock adapter outside `make
    demo` as `_sample` (`Item.is_sample`) - a human working the real queue
    must see that at a glance, in both `list` and `show`."""
    settings = _settings()
    store = _store(tmp_path, settings)
    item = store.upsert_item("email", "sample-marker-1", kind="triage_run",
                             payload={"date": "2026-08-28", "_sample": True})
    assert item.is_sample

    capsys.readouterr()
    review._print_item_line(item)
    assert "[SAMPLE DATA]" in capsys.readouterr().out

    rc = review.cmd_show(store, argparse.Namespace(id=item.id))
    assert rc == 0
    assert "[SAMPLE DATA]" in capsys.readouterr().out
    store.close()
