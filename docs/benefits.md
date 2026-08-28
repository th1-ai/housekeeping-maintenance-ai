# The business case

**Does.** Builds the optimal cleaning route from arrivals/departures/stayovers, predicts linen needs, logs maintenance tickets from guest messages, and triages them with context — a VIP arriving in five days bumps that room's repair up the list. Tracks turn-around time and keeps engineering holds visible on the live room board.

**Won't.** Doesn't replace the inspector's sign-off.

**Why.** Turn time is the constraint on same-day re-sells; maintenance issues hide in emails.

**Output.** Faster room turns + fewer missed maintenance items; protect occupancy on back-to-back nights.

**ROI.** -22% room-turn time (labor).

## What to measure

`tools/report.py` (`make report`) reads straight from `core.store` and
`hk_tickets`:

- **Route volume and cleaning hours.** Rooms in play, split by
  checkout/stayover/arrival, and the deep cleans protected inside them.
- **Walking-time saved.** Both the wing and the flat layout are costed on
  every run (`tools/engine.py:optimise_routes`), so you can watch the exact
  minutes and percentage wing routing is saving on your own property, not a
  demo's 500-room number. This is the number behind "-22% room-turn time" -
  measure it on your own floors for a few weeks before you quote it to
  anyone.
- **Escalation rate.** How many tickets a rule upgrades to high priority, and
  why (passport-in-safe, inspection deadline, trip hazard, HACCP, VIP
  arrival, day-sleeper blackout) - `tools/report.py`'s "by priority" line and
  the triage narrative both show this.
- **Contractor sign-off holds.** Tickets over `contractor_signoff_threshold`
  or on a contractor-only trade, held for the chief engineer rather than
  booked blind.
- **Tickets caught from guest email.** `tools/report.py`'s ticket count
  includes `source=guest_email` rows - the volume this agent adds beyond
  whatever your team already logs by hand.
- **Edit rate.** How often a human rewrites the narrative note
  (`tools/review.py edit`, recorded as a `learnings` row) versus approving it
  unchanged - the trust signal for whether to widen `mode: live`.
- **Spend.** `core.llm.complete()` records usage and cost per call; zero for
  `mock`/`interactive`, real for `claude-code`/`anthropic`
  (`Store.usage_totals()`).

## Honest caveats

- **No inspector sign-off gate.** The roster is explicit about this
  ("doesn't replace the inspector's sign-off"), and this template has no
  sellable/not-sellable checkpoint at all - a completed route or a completed
  ticket does not by itself mark a room ready to sell. That decision, and the
  PMS write behind it, stays with a person.
- **Linen forecasting is out of scope for this repo** - it lives in
  `procurement-supply-ai`'s circulating-stock handling in the source spec.
  Run that agent alongside this one if you want it.
- **The headcount, deep-clean cadence and parts-store notes are all
  configured, not measured.** `config/agent.yaml: housekeeping_headcount` is
  a number you set, not one the agent learns; deep-clean-due is a modulo
  stand-in for a real "stays since last deep clean" counter; parts-store
  strings are fixed text, not a live inventory. See docs/how-it-works.md
  "Design decisions" for what a fuller build would add.
- **The walking-time saving scales with room count.** On a small property the
  minutes saved are real but modest (a few floors, a few minutes); the
  mechanism is identical at 500 rooms, where it adds up to a genuinely
  different working day. Measure your own property before promising a
  percentage.
