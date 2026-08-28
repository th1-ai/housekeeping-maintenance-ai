"""Tests for tools/engine.py - the deterministic route and triage engines.

Pure unit tests, no store, no LLM, no fixtures on disk: every test builds its
own small Room/Ticket list so the rule under test is obvious from the test
itself.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools import engine  # noqa: E402

DEFAULT_RULES = {"wing_routing": True, "checkout_first": True, "deep_clean_cadence": True,
                 "maintenance_interleave": True, "contractor_threshold": True}
DEFAULT_CONFIG = {
    "shift_minutes": 390, "housekeeping_headcount": 6,
    "service_minutes": {"checkout": 35, "turn": 35, "stayover": 20, "arrival": 10},
    "walking": {"same_floor_minutes": 0.4, "floor_change_minutes": 2.0},
    "deep_clean_every": 21, "deep_clean_extra_minutes": 25,
    "contractor_signoff_threshold": 300, "engineer_start_hour": 9, "engineer_close_hour": 17,
    "travel_minutes_between_jobs": 15,
}


def _room(number, floor, status, vip=False):
    return engine.Room(room_number=number, floor=floor, status=status, vip=vip)


# --------------------------------------------------------------------------
# trade routing
# --------------------------------------------------------------------------
def test_trade_for_matches_refrigeration_before_the_general_fallback():
    info = engine.trade_for("Walk-in cooler gasket failing")
    assert info.trade == "commercial refrigeration"
    assert info.contractor_only is True
    assert info.minutes == 120
    assert info.parts_cost == 340.0


def test_trade_for_falls_back_to_general_maintenance():
    info = engine.trade_for("Wardrobe door hinge squeaks loudly")
    assert info.trade == "general maintenance"
    assert info.engineer_key == "mechanical"


def test_trade_for_in_room_safe_does_not_false_positive_on_unrelated_safe_wording():
    # "not safe to use" must not be mistaken for an in-room safe repair.
    info = engine.trade_for("Treadmill belt slips under load, not safe to use at pace")
    assert info.trade == "gym equipment"
    real_safe = engine.trade_for("The in-room safe battery died with the guest's passport inside")
    assert real_safe.trade == "in-room safe"


# --------------------------------------------------------------------------
# trade routing - other languages (SIMULATION.md finding 5: a Spanish-first
# property's own guest-reported tickets, unaccented the way a guest or a
# quick CSV import usually types them)
# --------------------------------------------------------------------------
def test_trade_for_understands_spanish_tickets_from_a_spanish_first_property():
    cases = [
        ("Aire acondicionado no enfria", "hvac"),
        ("Grifo del bano gotea", "plumbing"),
        ("Persiana atascada", "blinds and curtains"),
        ("Enchufe no funciona", "electrical"),
    ]
    for text, expected_trade in cases:
        info = engine.trade_for(text)
        assert info.trade == expected_trade, f"{text!r} routed to {info.trade!r}"

    # The identical AC problem in English must match the same trade -
    # proves the fix is additive, not a language switch.
    assert engine.trade_for("Air conditioning not cooling").trade == "hvac"

    # "Bomba de la piscina hace ruido" (pool pump) has no keyword in any
    # language - it falls to the general fallback exactly like the
    # identical problem phrased in English does. Not a language bug.
    assert engine.trade_for("Bomba de la piscina hace ruido").trade == "general maintenance"
    assert engine.trade_for("Pool pump makes noise").trade == "general maintenance"


def test_trade_for_understands_french_german_italian_portuguese():
    cases = [
        ("Climatisation en panne", "hvac"),                          # fr
        ("Robinet de la salle de bain qui fuit", "plumbing"),        # fr
        ("Klimaanlage kuhlt nicht", "hvac"),                         # de
        ("Wasserhahn tropft im Bad", "plumbing"),                    # de
        ("Aria condizionata non raffredda", "hvac"),                 # it
        ("Rubinetto del bagno che perde", "plumbing"),                # it
        ("Ar condicionado nao gela", "hvac"),                        # pt
        ("Torneira da casa de banho a pingar", "plumbing"),          # pt
    ]
    for text, expected_trade in cases:
        info = engine.trade_for(text)
        assert info.trade == expected_trade, f"{text!r} routed to {info.trade!r}"


def test_trade_for_folds_accents_so_typed_and_unaccented_text_match_the_same_rule():
    with_accents = engine.trade_for("El baño tiene una fuga en el grifo")
    without_accents = engine.trade_for("El bano tiene una fuga en el grifo")
    assert with_accents.trade == without_accents.trade == "plumbing"


# --------------------------------------------------------------------------
# escalation
# --------------------------------------------------------------------------
def test_escalation_passport_in_dead_safe():
    ticket = engine.Ticket(id="t1", room="103", summary="Guest locked passport in safe",
                           detail="The in-room safe battery died with the passport inside.")
    esc = engine.escalation_for(ticket, set())
    assert esc is not None
    assert "passport" in esc.reason.lower()


def test_escalation_inspection_today_extracts_the_time():
    ticket = engine.Ticket(id="t2", room="208", summary="Squeaky hinge",
                           detail="Standards inspection at 14:00 today.")
    esc = engine.escalation_for(ticket, set())
    assert esc is not None
    assert "14:00" in esc.reason


def test_escalation_vip_arrival_requires_a_confirmed_vip_name():
    ticket = engine.Ticket(id="t3", room="107", summary="Balcony door slider sticking",
                           detail="Elena Marchetti arrives in 5 days.")
    assert engine.escalation_for(ticket, set()) is None  # not a known VIP -> no escalation
    esc = engine.escalation_for(ticket, {"elena marchetti"})
    assert esc is not None
    assert "Elena Marchetti" in esc.reason


def test_escalation_none_for_an_ordinary_ticket():
    ticket = engine.Ticket(id="t4", room="204", summary="Air conditioning not cooling",
                           detail="Blows warm air only.")
    assert engine.escalation_for(ticket, set()) is None


# --------------------------------------------------------------------------
# triage scheduling
# --------------------------------------------------------------------------
def test_triage_tickets_upgrades_priority_and_counts_escalations():
    tickets = [
        engine.Ticket(id="t1", room="103", summary="Passport in safe",
                     detail="The in-room safe battery died with the passport inside.",
                     priority="medium"),
        engine.Ticket(id="t2", room="204", summary="AC not cooling", priority="medium"),
    ]
    result = engine.triage_tickets(tickets, rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    by_id = {d.ticket_id: d for d in result.decisions}
    assert by_id["t1"].priority == "high"
    assert by_id["t1"].upgraded is True
    assert by_id["t2"].priority == "medium"
    assert result.escalated_count == 1


def test_triage_tickets_holds_contractor_jobs_for_signoff_over_threshold():
    tickets = [engine.Ticket(id="t1", room="Kitchen Walk-in", summary="Walk-in cooler drifting",
                            detail="HACCP log flagged an overnight drift.", priority="medium")]
    on = engine.triage_tickets(tickets, rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    assert on.decisions[0].held_for_signoff is True
    assert on.contractor_held_count == 1

    off_rules = dict(DEFAULT_RULES, contractor_threshold=False)
    off = engine.triage_tickets(tickets, rules=off_rules, config=DEFAULT_CONFIG)
    assert off.decisions[0].held_for_signoff is False
    assert "no second pair of eyes" in off.decisions[0].schedule_label.lower()


def test_triage_tickets_batches_low_priority_into_friday_regardless_of_clock():
    tickets = [engine.Ticket(id="t1", room="303", summary="Fan squeaking", priority="low")]
    result = engine.triage_tickets(tickets, rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    assert "Friday" in result.decisions[0].schedule_label
    assert result.low_priority_count == 1


def test_triage_tickets_schedules_two_engineers_independently():
    tickets = [
        engine.Ticket(id="t1", room="101", summary="Tap dripping", priority="medium"),   # mechanical
        engine.Ticket(id="t2", room="102", summary="Light switch broken", priority="medium"),  # electrical
    ]
    result = engine.triage_tickets(tickets, rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    # Both start at the same 09:00 clock since they are on different engineers.
    assert result.decisions[0].schedule_label == "09:00"
    assert result.decisions[1].schedule_label == "09:00"


# --------------------------------------------------------------------------
# route optimiser
# --------------------------------------------------------------------------
def test_optimise_routes_counts_in_play_rooms_and_ignores_vacant():
    rooms = [_room("101", 1, "checkout"), _room("102", 1, "vacant"), _room("103", 1, "stayover")]
    plan = engine.optimise_routes(rooms, [], rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    assert plan.in_play_count == 2
    assert plan.total_rooms == 3


def test_optimise_routes_checkout_first_orders_departures_before_stayovers():
    rooms = [_room("101", 1, "stayover"), _room("102", 1, "checkout")]
    plan = engine.optimise_routes(rooms, [], rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    first_route = plan.routes[0]
    assert first_route.all_rooms[0] == "102"  # the checkout, even though 101 sorts first by number

    off_rules = dict(DEFAULT_RULES, checkout_first=False)
    plan_off = engine.optimise_routes(rooms, [], rules=off_rules, config=DEFAULT_CONFIG)
    assert plan_off.routes[0].all_rooms[0] == "101"  # room number only


def test_optimise_routes_deep_clean_cadence_adds_extra_minutes():
    # 21 % 21 == 0 with day_offset 0, so room 21 is due a deep clean.
    rooms = [_room("21", 1, "checkout")]
    plan = engine.optimise_routes(rooms, [], rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    stop = plan.routes[0].stops[0]
    assert stop.deep_clean is True
    assert stop.minutes == 35 + 25

    off_rules = dict(DEFAULT_RULES, deep_clean_cadence=False)
    plan_off = engine.optimise_routes(rooms, [], rules=off_rules, config=DEFAULT_CONFIG)
    assert plan_off.routes[0].stops[0].deep_clean is False


def test_optimise_routes_wing_vs_flat_are_both_costed_every_run():
    rooms = [_room(str(100 + i), 1, "checkout") for i in range(5)] + \
        [_room(str(200 + i), 2, "checkout") for i in range(5)]
    plan = engine.optimise_routes(rooms, [], rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    assert plan.wing_walking_minutes >= 0
    assert plan.flat_walking_minutes >= 0
    assert plan.strategy_used == "wing"
    # Wing never crosses a floor, so it can only ever cost the same or less.
    assert plan.wing_walking_minutes <= plan.flat_walking_minutes


def test_optimise_routes_vip_flag_is_independent_of_rule_toggles():
    rooms = [_room("109", 1, "arrival", vip=True)]
    all_rules_off = {k: False for k in DEFAULT_RULES}
    plan = engine.optimise_routes(rooms, [], rules=all_rules_off, config=DEFAULT_CONFIG)
    assert len(plan.vip_flags) == 1
    assert plan.vip_flags[0]["room"] == "109"


def test_optimise_routes_capacity_warning_fires_over_headcount():
    rooms = [_room(str(100 + i), 1, "checkout") for i in range(30)]
    tight_config = dict(DEFAULT_CONFIG, housekeeping_headcount=1)
    plan = engine.optimise_routes(rooms, [], rules=DEFAULT_RULES, config=tight_config)
    assert plan.capacity_warning is not None
    assert "agency" in plan.capacity_warning


def test_optimise_routes_interleaves_a_high_priority_ticket_into_its_route():
    rooms = [_room("101", 1, "checkout"), _room("102", 1, "stayover")]
    ticket = engine.Ticket(id="m1", room="102", summary="AC not cooling")
    plan = engine.optimise_routes(rooms, [ticket], rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    slots = {m.ticket_id: m for m in plan.maintenance_slots}
    assert slots["m1"].kind == "interleaved"
    assert ":" in slots["m1"].slot


def test_optimise_routes_flags_a_non_numeric_room_as_direct_and_off_board_as_unscheduled():
    rooms = [_room("101", 1, "checkout")]
    tickets = [engine.Ticket(id="pub", room="Sauna Plant", summary="Seal failing"),
              engine.Ticket(id="off", room="999", summary="Light switch broken")]
    plan = engine.optimise_routes(rooms, tickets, rules=DEFAULT_RULES, config=DEFAULT_CONFIG)
    kinds = {m.ticket_id: m.kind for m in plan.maintenance_slots}
    assert kinds["pub"] == "direct"
    assert kinds["off"] == "unscheduled"


def test_ceil_to_quarter_hour_rounds_up():
    assert engine.ceil_to_quarter_hour(0) == "08:00"
    assert engine.ceil_to_quarter_hour(1) == "08:15"
    assert engine.ceil_to_quarter_hour(150) == "10:30"
