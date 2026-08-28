"""tools/engine.py - The Steward's deterministic engines: routes and triage.

Pure functions over dataclasses, no I/O (ARCHITECTURE.md section 1 and
factory/workflows/build-repo.md section 2: "the demo engines are deliberately
LLM-free: rules, formulas, thresholds and state machines decide; the model
classifies free text and writes prose"). tools/routes.py and tools/triage.py
read the room board, the ticket table and the PMS, call the functions here,
then hand the result to core.llm.complete() for a short narrative - the model
never touches the numbers below.

Two engines:

``optimise_routes``  builds today's cleaning routes from the room board, costs
                     both the wing (one floor at a time) and flat (one shared
                     worklist) layouts on every run so the walking-time saving
                     is provable, and interleaves any high-priority
                     maintenance ticket into the route that owns its room.

``triage_tickets``   routes each open ticket to a trade and an engineer,
                     re-scores its priority against a fixed set of safety and
                     VIP rules, and schedules it - or holds it for the chief
                     engineer's sign-off when it needs a contractor.

Every decision here can be read back as one line of "why" - see
``thinking_log`` on both results and docs/how-it-works.md.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# shared inputs
# --------------------------------------------------------------------------
STATUSES_IN_PLAY = ("checkout", "turn", "stayover", "arrival")
DEFAULT_SERVICE_MINUTES = {"checkout": 35, "turn": 35, "stayover": 20, "arrival": 10}
DEPARTURE_ORDER = {"checkout": 0, "turn": 0, "arrival": 1, "stayover": 2}


@dataclass
class Room:
    """One row of today's room board (fixtures/hotel/room_status.json)."""

    room_number: str
    floor: int
    room_type: str = ""
    status: str = "vacant"
    vip: bool = False
    note: str = ""

    @property
    def is_numeric(self) -> bool:
        return self.room_number.isdigit()

    @property
    def in_play(self) -> bool:
        return self.status in STATUSES_IN_PLAY


@dataclass
class Ticket:
    """The fields the engine needs from an ``hk_tickets`` row."""

    id: str
    room: str
    summary: str
    detail: str = ""
    priority: str = "medium"


# --------------------------------------------------------------------------
# route optimiser - dataclasses
# --------------------------------------------------------------------------
@dataclass
class RouteStop:
    room_number: str
    floor: int
    kind: str
    minutes: int
    deep_clean: bool = False
    vip: bool = False
    walk_in: float = 0.0
    start_offset: float = 0.0  # minutes after 08:00 when the attendant arrives


@dataclass
class RouteCard:
    attendant: int
    stops: list = field(default_factory=list)

    @property
    def floors(self) -> list[int]:
        seen: list[int] = []
        for s in self.stops:
            if s.floor not in seen:
                seen.append(s.floor)
        return seen

    @property
    def room_count(self) -> int:
        return len(self.stops)

    @property
    def service_minutes(self) -> int:
        return sum(s.minutes for s in self.stops)

    @property
    def walking_minutes(self) -> float:
        return round(sum(s.walk_in for s in self.stops), 1)

    @property
    def counts(self) -> dict:
        out = {"full_clean": 0, "stayover": 0, "arrival": 0, "deep_clean": 0, "vip": 0}
        for s in self.stops:
            out["full_clean" if s.kind in ("checkout", "turn") else s.kind] = (
                out.get("full_clean" if s.kind in ("checkout", "turn") else s.kind, 0) + 1)
            if s.deep_clean:
                out["deep_clean"] += 1
            if s.vip:
                out["vip"] += 1
        return out

    @property
    def first_rooms(self) -> list[str]:
        return [s.room_number for s in self.stops[:6]]

    @property
    def all_rooms(self) -> list[str]:
        return [s.room_number for s in self.stops]


@dataclass
class MaintenanceSlot:
    ticket_id: str
    room: str
    kind: str          # interleaved | direct | unscheduled
    slot: str = ""
    note: str = ""


@dataclass
class RoutePlan:
    day_offset: int
    total_rooms: int
    in_play_count: int
    counts: dict
    routes: list
    strategy_used: str
    wing_walking_minutes: float
    flat_walking_minutes: float
    minutes_saved: float
    percent_saved: float
    cleaning_hours: float
    vip_flags: list
    maintenance_slots: list
    capacity_warning: str | None
    thinking_log: list


def ceil_to_quarter_hour(minutes_from_open: float, open_hour: int = 8) -> str:
    """``08:00 + minutes`` rounded up to the next quarter hour, as ``HH:MM``."""
    q = int(math.ceil(max(minutes_from_open, 0) / 15.0)) * 15
    total = open_hour * 60 + q
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _order_key(room: Room, checkout_first: bool) -> tuple:
    if checkout_first:
        return (DEPARTURE_ORDER.get(room.status, 3), int(room.room_number)
                if room.is_numeric else 0, room.room_number)
    return (int(room.room_number) if room.is_numeric else 0, room.room_number)


def _minutes_for(room: Room, *, service_minutes: dict, deep_clean_every: int,
                 deep_clean_extra: int, day_offset: int) -> tuple[int, bool]:
    base = service_minutes.get(room.status, 0)
    due = (room.status in ("checkout", "turn") and room.is_numeric
          and (int(room.room_number) + day_offset) % deep_clean_every == 0)
    if due:
        return base + deep_clean_extra, True
    return base, False


def _stamp_offsets(stops: list[RouteStop]) -> None:
    """Fill in ``start_offset`` on each stop: arrival time within its route."""
    cum = 0.0
    for stop in stops:
        cum += stop.walk_in
        stop.start_offset = cum
        cum += stop.minutes


def _pack_wing(ordered: list[Room], *, cap: int, same_floor: float,
              service_minutes: dict, deep_clean_every: int, deep_clean_extra: int,
              day_offset: int) -> list[list[RouteStop]]:
    """One floor at a time: the attendant never re-crosses the lift."""
    by_floor: dict[int, list[Room]] = {}
    for room in ordered:
        by_floor.setdefault(room.floor, []).append(room)

    routes: list[list[RouteStop]] = []
    for floor in sorted(by_floor):
        current: list[RouteStop] = []
        minutes_used = 0.0
        for room in by_floor[floor]:
            minutes, deep = _minutes_for(room, service_minutes=service_minutes,
                                         deep_clean_every=deep_clean_every,
                                         deep_clean_extra=deep_clean_extra,
                                         day_offset=day_offset)
            walk = 0.0 if not current else same_floor
            if current and minutes_used + minutes + walk > cap:
                routes.append(current)
                current, minutes_used, walk = [], 0.0, 0.0
            current.append(RouteStop(room_number=room.room_number, floor=room.floor,
                                     kind=room.status, minutes=minutes, deep_clean=deep,
                                     vip=room.vip, walk_in=walk))
            minutes_used += minutes + walk
        if current:
            routes.append(current)
    for route in routes:
        _stamp_offsets(route)
    return routes


def _pack_flat(ordered: list[Room], *, cap: int, same_floor: float, floor_change: float,
              service_minutes: dict, deep_clean_every: int, deep_clean_extra: int,
              day_offset: int) -> list[list[RouteStop]]:
    """One shared worklist: each room goes to whichever attendant is lightest."""
    routes: list[list[RouteStop]] = [[]]
    minutes_used = [0.0]
    last_floor: list[int | None] = [None]

    for room in ordered:
        minutes, deep = _minutes_for(room, service_minutes=service_minutes,
                                     deep_clean_every=deep_clean_every,
                                     deep_clean_extra=deep_clean_extra,
                                     day_offset=day_offset)
        candidates = [i for i in range(len(routes)) if minutes_used[i] + minutes <= cap]
        if candidates:
            i = min(candidates, key=lambda i: minutes_used[i])
        else:
            routes.append([])
            minutes_used.append(0.0)
            last_floor.append(None)
            i = len(routes) - 1
        walk = 0.0 if last_floor[i] is None else (
            same_floor if last_floor[i] == room.floor else floor_change)
        routes[i].append(RouteStop(room_number=room.room_number, floor=room.floor,
                                   kind=room.status, minutes=minutes, deep_clean=deep,
                                   vip=room.vip, walk_in=walk))
        minutes_used[i] += minutes + walk
        last_floor[i] = room.floor

    routes = [r for r in routes if r]
    for route in routes:
        _stamp_offsets(route)
    return routes


def _walking_total(routes: list[list[RouteStop]]) -> float:
    return round(sum(s.walk_in for route in routes for s in route), 1)


def _find_stop(routes: list[list[RouteStop]], room_number: str) -> RouteStop | None:
    for route in routes:
        for stop in route:
            if stop.room_number == room_number:
                return stop
    return None


def optimise_routes(rooms: list[Room], high_priority_tickets: list[Ticket], *,
                    rules: dict, config: dict, day_offset: int = 0) -> RoutePlan:
    """Build today's cleaning routes. See the module docstring for the shape.

    ``rules`` is the five hk_rules toggles (``wing_routing``, ``checkout_first``,
    ``deep_clean_cadence``, ``maintenance_interleave`` - ``contractor_threshold``
    belongs to :func:`triage_tickets`). ``config`` is agent.yaml's numeric knobs
    (service_minutes, walking, deep_clean_every/extra, shift_minutes,
    housekeeping_headcount).
    """
    service_minutes = dict(DEFAULT_SERVICE_MINUTES, **(config.get("service_minutes") or {}))
    cap = int(config.get("shift_minutes", 390))
    same_floor = float((config.get("walking") or {}).get("same_floor_minutes", 0.4))
    floor_change = float((config.get("walking") or {}).get("floor_change_minutes", 2.0))
    deep_clean_every = int(config.get("deep_clean_every", 21)) if rules.get(
        "deep_clean_cadence", True) else 10 ** 9  # effectively never due when the rule is off
    deep_clean_extra = int(config.get("deep_clean_extra_minutes", 25))
    checkout_first = bool(rules.get("checkout_first", True))
    wing_routing = bool(rules.get("wing_routing", True))
    headcount = int(config.get("housekeeping_headcount", 6))

    in_play = [r for r in rooms if r.in_play]
    ordered = sorted(in_play, key=lambda r: _order_key(r, checkout_first))

    wing_routes = _pack_wing(ordered, cap=cap, same_floor=same_floor,
                             service_minutes=service_minutes,
                             deep_clean_every=deep_clean_every,
                             deep_clean_extra=deep_clean_extra, day_offset=day_offset)
    flat_routes = _pack_flat(ordered, cap=cap, same_floor=same_floor,
                             floor_change=floor_change, service_minutes=service_minutes,
                             deep_clean_every=deep_clean_every,
                             deep_clean_extra=deep_clean_extra, day_offset=day_offset)
    wing_walk = _walking_total(wing_routes)
    flat_walk = _walking_total(flat_routes)

    chosen = wing_routes if wing_routing else flat_routes
    strategy = "wing" if wing_routing else "flat"
    saved = flat_walk - wing_walk if wing_routing else wing_walk - flat_walk
    total_service = sum(s.minutes for route in chosen for s in route)
    percent_saved = round((saved / total_service) * 100, 1) if total_service and saved > 0 else 0.0

    routes = [RouteCard(attendant=i + 1, stops=stops) for i, stops in enumerate(chosen)]

    counts = {"checkout": 0, "turn": 0, "stayover": 0, "arrival": 0, "deep_clean": 0}
    for room in in_play:
        counts[room.status] = counts.get(room.status, 0) + 1
    cleaning_hours = round(total_service / 60, 1)

    vip_flags = [{"room": r.room_number, "note": r.note or "VIP - supervisor re-check"}
                for r in in_play if r.vip]

    maintenance_slots: list[MaintenanceSlot] = []
    if rules.get("maintenance_interleave", True):
        for ticket in high_priority_tickets:
            if not ticket.room.isdigit():
                maintenance_slots.append(MaintenanceSlot(
                    ticket_id=ticket.id, room=ticket.room, kind="direct",
                    note="Not a guest room - housekeeping only needs the area closed."))
                continue
            stop = _find_stop(chosen, ticket.room)
            if stop is None:
                maintenance_slots.append(MaintenanceSlot(
                    ticket_id=ticket.id, room=ticket.room, kind="unscheduled",
                    note=f"Room {ticket.room} is high priority but not on today's board - "
                        "flag it to the supervisor directly."))
                continue
            slot = ceil_to_quarter_hour(stop.start_offset)
            maintenance_slots.append(MaintenanceSlot(
                ticket_id=ticket.id, room=ticket.room, kind="interleaved", slot=slot,
                note=f"{ticket.room} {ticket.summary.lower()} - engineering slot {slot}, "
                    "clean after."))
    else:
        unscheduled = len(high_priority_tickets)
        if unscheduled:
            maintenance_slots.append(MaintenanceSlot(
                ticket_id="", room="", kind="unscheduled",
                note=f"Maintenance interleave is off - engineering and housekeeping run "
                    f"blind to each other ({unscheduled} high-priority ticket(s))."))

    capacity_warning = None
    if len(routes) > headcount:
        capacity_warning = (
            f"{len(routes)} routes needed but the team models {headcount} attendants - "
            "the overflow has to go to agency or the stayovers slip to tomorrow.")

    if wing_routing:
        walking_line = (
            f"Wing routing: {wing_walk} min walking across the house vs {flat_walk} min "
            f"on the flat worklist - {abs(round(flat_walk - wing_walk, 1))} min saved, "
            f"{percent_saved}% of the day's cleaning time back on the rooms.")
    else:
        walking_line = (
            f"Wing routing would cost {wing_walk} min walking; flat worklist in use "
            f"costs {flat_walk} min (wing routing is off).")

    log = [
        f"Reading the PMS board - {len(in_play)} of {len(rooms)} rooms need a visit "
        f"({counts.get('checkout', 0) + counts.get('turn', 0)} full cleans, "
        f"{counts.get('stayover', 0)} stayovers, {counts.get('arrival', 0)} arrival checks).",
        f"Ordering: {'checkout-first' if checkout_first else 'room number only'}.",
        walking_line,
        f"{len(routes)} route(s), {len(in_play)} rooms, {cleaning_hours} cleaning hours.",
    ]
    if vip_flags:
        log.append(f"VIP re-check: {', '.join(f['room'] for f in vip_flags)}.")
    if maintenance_slots:
        log.append(f"Maintenance interleaved: {sum(1 for m in maintenance_slots if m.kind == 'interleaved')}, "
                   f"direct to engineering: {sum(1 for m in maintenance_slots if m.kind == 'direct')}, "
                   f"unscheduled: {sum(1 for m in maintenance_slots if m.kind == 'unscheduled')}.")
    log.append(capacity_warning or "No capacity warning.")

    return RoutePlan(
        day_offset=day_offset, total_rooms=len(rooms), in_play_count=len(in_play),
        counts=counts, routes=routes, strategy_used=strategy,
        wing_walking_minutes=wing_walk, flat_walking_minutes=flat_walk,
        minutes_saved=round(saved, 1), percent_saved=percent_saved,
        cleaning_hours=cleaning_hours, vip_flags=vip_flags,
        maintenance_slots=maintenance_slots, capacity_warning=capacity_warning,
        thinking_log=log)


# --------------------------------------------------------------------------
# ticket triage - dataclasses
# --------------------------------------------------------------------------
@dataclass
class TradeInfo:
    trade: str
    engineer_key: str          # "mechanical" | "electrical" - config resolves the name
    minutes: int
    parts_cost: float = 0.0
    parts_note: str = ""
    contractor_only: bool = False
    lead_time_note: str = ""


@dataclass
class Escalation:
    reason: str


@dataclass
class TriageDecision:
    ticket_id: str
    room: str
    summary: str
    trade: str
    engineer_key: str
    priority: str
    upgraded: bool
    reason: str
    minutes: int
    parts_cost: float
    parts_note: str
    lead_time_note: str
    contractor: bool
    held_for_signoff: bool
    schedule_label: str


@dataclass
class TriageResult:
    decisions: list
    escalated_count: int
    contractor_held_count: int
    low_priority_count: int
    thinking_log: list


# Checked in order, first match wins - see docs/how-it-works.md for why the
# order matters (refrigeration and safe both have to beat the general
# maintenance fallback, for example).
#
# Each pattern carries English plus Spanish, French, German, Italian and
# Portuguese keywords for the same fault, so a ticket logged in any of a
# hotel's `hotel.languages` (or any guest's own language, via
# `tools/ticket_intake.py`) routes to the same trade as the identical
# problem phrased in English - see docs/how-it-works.md "Trade routing and
# escalation". `trade_for()` accent-folds the input first (`_fold()`), so
# "bano"/"baño", "camara"/"câmara" etc. all match the same keyword.
_TRADE_RULES: list[tuple[re.Pattern, TradeInfo]] = [
    (re.compile(r"\b(gasket|walk-in|walk in|cold room|"
               r"junta|camara frigorifica|cuarto frio|camara fria|"  # es/pt
               r"joint|chambre froide|"  # fr
               r"dichtung|kuhlraum|kuhlzelle|"  # de
               r"guarnizione|cella frigorifera|camera fredda)\b", re.I),  # it
     TradeInfo("commercial refrigeration", "mechanical", 120, 340.0,
               "not a stock item", contractor_only=True)),
    (re.compile(r"\b(in-room safe|room safe|the safe|safe battery|"
               r"caja fuerte|caja de seguridad|"  # es
               r"coffre-fort|coffre|"  # fr
               r"der safe|zimmersafe|tresor|"  # de - not bare "safe": collides
               r"cassaforte|"  # it     with the English adjective ("not safe to use")
               r"cofre)\b", re.I),  # pt
     TradeInfo("in-room safe", "electrical", 20, 0.0, "")),
    (re.compile(r"\b(sauna|steam room|plant room|"
               r"bano turco|sala de maquinas|"  # es/pt
               r"hammam|salle des machines|"  # fr
               r"dampfbad|technikraum|"  # de
               r"bagno turco|sala macchine)\b", re.I),  # it
     TradeInfo("spa plant", "mechanical", 60, 85.0, "")),
    (re.compile(r"\b(hvac|air.?conditioning|a/c|\bac\b|"
               r"aire acondicionado|climatizacion|"  # es
               r"climatisation|climatiseur|"  # fr
               r"klimaanlage|"  # de
               r"aria condizionata|climatizzatore|"  # it
               r"ar condicionado|climatizacao)\b", re.I),  # pt
     TradeInfo("hvac", "mechanical", 90, 45.0, "capacitor and filter kit")),
    (re.compile(r"\b(minibar|mini-bar|frigobar)\b", re.I),
     TradeInfo("minibar refrigeration", "electrical", 45, 0.0, "")),
    (re.compile(r"\b(tap|basin|mixer|drain|shower|"
               r"grifo|lavabo|desague|ducha|"  # es
               r"robinet|evier|douche|fuite|"  # fr
               r"wasserhahn|waschbecken|abfluss|dusche|"  # de
               r"rubinetto|lavandino|scarico|doccia|"  # it
               r"torneira|lavatorio|ralo|chuveiro)\b", re.I),  # pt
     TradeInfo("plumbing", "mechanical", 30, 18.0, "basin cartridge")),
    (re.compile(r"\b(light|lamp|bulb|socket|switch|led|fan|"
               r"luz|lampara|bombilla|enchufe|interruptor|ventilador|"  # es
               r"lumiere|lampe|ampoule|prise|interrupteur|ventilateur|"  # fr
               r"gluhbirne|steckdose|schalter|ventilator|"  # de
               r"lampadina|presa|interruttore|ventilatore|luce|lampada|"  # it
               r"tomada|ventoinha)\b", re.I),  # pt
     TradeInfo("electrical", "electrical", 25, 12.0, "LED driver")),
    (re.compile(r"\b(doorbell|chime|"
               r"timbre|"  # es
               r"sonnette|"  # fr
               r"turklingel|klingel|"  # de
               r"campanello|"  # it
               r"campainha)\b", re.I),  # pt
     TradeInfo("doorbell", "electrical", 30, 40.0, "")),
    (re.compile(r"\b(tv|television|no signal|"
               r"sin senal|no hay senal|"  # es
               r"pas de signal|"  # fr
               r"fernseher|kein signal|"  # de
               r"nessun segnale|"  # it
               r"televisao|sem sinal)\b", re.I),  # pt
     TradeInfo("av/tv", "electrical", 30, 0.0, "")),
    (re.compile(r"\b(blind|curtain|motor|"
               r"persiana|cortina|atascada|atascado|atasco|"  # es
               r"store|rideau|volet|"  # fr
               r"jalousie|vorhang|"  # de
               r"tapparella|tenda|"  # it
               r"estore)\b", re.I),  # pt
     TradeInfo("blinds and curtains", "mechanical", 50, 0.0, "",
               lead_time_note="2-day supplier lead, not a stock item")),
    (re.compile(r"\b(treadmill|gym equipment|drive belt|"
               r"cinta de correr|gimnasio|"  # es
               r"tapis de course|"  # fr
               r"laufband|"  # de
               r"tapis roulant|"  # it
               r"esteira)\b", re.I),  # pt
     TradeInfo("gym equipment", "mechanical", 60, 180.0, "drive belt")),
    (re.compile(r"\b(balcony|slider|"
               r"balcon|puerta corredera|"  # es/fr
               r"schiebetur|"  # de
               r"balcone|porta scorrevole|"  # it
               r"varanda|porta de correr)\b", re.I),  # pt
     TradeInfo("carpentry", "mechanical", 35, 0.0, "")),
    (re.compile(r"\b(decking|"
               r"tarima|terraza de madera|"  # es
               r"terrasse en bois|"  # fr
               r"terrassendielen|"  # de
               r"pavimentazione in legno|"  # it
               r"deck de madeira)\b", re.I),  # pt
     TradeInfo("carpentry", "mechanical", 45, 22.0, "")),
]
_FALLBACK_TRADE = TradeInfo("general maintenance", "mechanical", 45, 0.0, "")

_VIP_ARRIVAL_RE = re.compile(
    r"([A-Z][a-z]+(?:[- ][A-Z][a-z]+)+) arrives?(?: in| within)? (\d+) days?")


def _fold(text: str) -> str:
    """Strip accents so "bano"/"baño", "camara"/"câmara" match one keyword.

    `trade_for()` runs this before matching so a ticket does not need to be
    typed with the right accents to route correctly - guest email and staff
    notes rarely are.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def trade_for(text: str) -> TradeInfo:
    """First-match trade lookup over a ticket's summary + detail.

    Understands the keyword for each fault in English, Spanish, French,
    German, Italian and Portuguese (see `_TRADE_RULES`) - at minimum, the
    languages a hotel is likely to configure in `hotel.languages`.
    """
    folded = _fold(text)
    for pattern, info in _TRADE_RULES:
        if pattern.search(folded):
            return info
    return _FALLBACK_TRADE


def escalation_for(ticket: Ticket, vip_names: set[str]) -> Escalation | None:
    """Safety/compliance/VIP rules that override the reported priority.

    Checked in priority order; the first match wins, matching the demo this
    was ported from (docs/how-it-works.md has the source spec's exact order).
    ``vip_names`` is a set of lower-cased guest full names the PMS confirms
    are VIP and have an upcoming reservation - built by tools/triage.py, never
    looked up here, so this function stays a pure text-in/reason-out check.
    """
    text = f"{ticket.summary} {ticket.detail}"
    low = text.lower()
    if re.search(r"\bsafe\b", low) and re.search(r"\bpassport\b", low):
        return Escalation("A passport is locked in a dead safe - the guest needs it "
                          "back, so this jumps the queue.")
    m = re.search(r"inspection at (\d{1,2}:\d{2})", low)
    if m:
        return Escalation(f"Standards inspection today at {m.group(1)} - this has to "
                          "be done before then.")
    if re.search(r"\btrip\b|\bslip\b|\bhazard(?:ous)?\b", low):
        return Escalation("A trip or slip hazard - this is a safety issue, not a "
                          "comfort complaint.")
    if re.search(r"\bhaccp\b|\bwalk-in\b|\bwalk in\b|\bfood safety\b|\bcold room\b", low):
        return Escalation("Food safety - a walk-in or cold-room issue is HACCP "
                          "territory, not a comfort complaint.")
    m = _VIP_ARRIVAL_RE.search(text)
    if m and m.group(1).lower() in vip_names:
        return Escalation(f"{m.group(1)} (VIP) arrives in {m.group(2)} days - the room "
                          "has to be perfect on the day, so this moves ahead of "
                          "routine work.")
    if (re.search(r"\bblackout\b", low) and re.search(r"\bblind\b", low)
            and re.search(r"\bpilot\b|\bday.?sleeper\b", low)):
        return Escalation("Guest is a pilot on a day-sleeper booking and needs full "
                          "blackout - this cannot wait.")
    return None


def triage_tickets(tickets: list[Ticket], *, rules: dict, config: dict,
                   vip_names: set[str] | None = None) -> TriageResult:
    """Trade-route, re-score and schedule every open ticket.

    ``rules`` needs ``contractor_threshold`` (the other four belong to
    :func:`optimise_routes`). ``config`` needs ``contractor_signoff_threshold``,
    ``engineer_start_hour``, ``engineer_close_hour`` and
    ``travel_minutes_between_jobs``.
    """
    vip_names = vip_names or set()
    threshold = float(config.get("contractor_signoff_threshold", 300))
    start_hour = int(config.get("engineer_start_hour", 9))
    close_hour = int(config.get("engineer_close_hour", 17))
    travel = int(config.get("travel_minutes_between_jobs", 15))
    contractor_rule_on = bool(rules.get("contractor_threshold", True))

    clock: dict[str, float] = {"mechanical": 0.0, "electrical": 0.0}
    decisions: list[TriageDecision] = []
    escalated = contractor_held = low_priority = 0
    log: list[str] = []

    for ticket in tickets:
        trade = trade_for(f"{ticket.summary} {ticket.detail}")
        esc = escalation_for(ticket, vip_names)
        priority = ticket.priority
        upgraded = False
        reason = ""
        if esc is not None:
            reason = esc.reason
            if priority != "high":
                priority = "high"
                upgraded = True
                escalated += 1
                log.append(f"{ticket.room}: upgraded to high - {esc.reason}")

        contractor = trade.contractor_only or trade.parts_cost > threshold
        held = False
        if contractor:
            if contractor_rule_on:
                schedule_label = ("Held - chief engineer sign-off, then first "
                                  "contractor slot tomorrow 08:00")
                held = True
                contractor_held += 1
            else:
                schedule_label = "Contractor booked today at 16:00 - no second pair of eyes"
        elif priority == "low":
            schedule_label = "Friday - the planned-maintenance round"
            low_priority += 1
        else:
            elapsed = clock[trade.engineer_key]
            start_dec = start_hour + elapsed / 60
            if start_dec >= close_hour:
                schedule_label = f"Tomorrow {start_hour:02d}:00"
                clock[trade.engineer_key] = trade.minutes + travel
            else:
                hh, mm = int(start_dec), int(round((start_dec - int(start_dec)) * 60))
                schedule_label = f"{hh:02d}:{mm:02d}"
                clock[trade.engineer_key] = elapsed + trade.minutes + travel

        decisions.append(TriageDecision(
            ticket_id=ticket.id, room=ticket.room, summary=ticket.summary,
            trade=trade.trade, engineer_key=trade.engineer_key, priority=priority,
            upgraded=upgraded, reason=reason, minutes=trade.minutes,
            parts_cost=trade.parts_cost, parts_note=trade.parts_note,
            lead_time_note=trade.lead_time_note, contractor=contractor,
            held_for_signoff=held, schedule_label=schedule_label))

    log.insert(0, f"Triaged {len(tickets)} ticket(s): {escalated} escalated, "
                 f"{contractor_held} held for sign-off, {low_priority} to Friday.")
    return TriageResult(decisions=decisions, escalated_count=escalated,
                        contractor_held_count=contractor_held,
                        low_priority_count=low_priority, thinking_log=log)
