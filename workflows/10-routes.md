# Workflow: today's cleaning routes

Objective: build today's route plan, understand what it is telling you, and
dispatch it once a person has looked it over.

## Inputs

- The room board: `data/imports/room_status.csv` if it exists, else
  `fixtures/hotel/room_status.json` (see `workflows/00-setup.md` step 5).
- `config/agent.yaml`'s `rules` (`wing_routing`, `checkout_first`,
  `deep_clean_cadence`, `maintenance_interleave`) and the numeric knobs
  (`shift_minutes`, `service_minutes`, `walking`, `deep_clean_every`,
  `housekeeping_headcount`).
- Today's high-priority tickets (`workflows/11-triage.md`) - a route plan
  built without them still works, it just has nothing to interleave.

## Steps

1. **Build the plan.**
   ```bash
   python3 tools/routes.py optimise
   python3 tools/routes.py optimise --day-offset 1     # tomorrow, for planning ahead
   ```
   `tools/run.py --once` does this automatically, right after triage, every
   pass - see `workflows/11-triage.md`. Running `tools/routes.py` on its own
   still triages first internally (read-only) so the interleave is accurate.

2. **If `llm.provider` is `interactive`,** the run stops with exit code 3 and
   parks a prompt in `data/pending/`. Read `*.prompt.md`, write your answer
   as JSON to the matching `*.answer.json`, and run the same command again.

3. **Read the thinking log.** Every line is printed to the terminal and
   stored on the item: how many rooms are in play, the wing-vs-flat walking
   comparison (both are always costed, whichever is switched on), any VIP
   re-check, any maintenance interleaved into a route, any capacity warning.

4. **See what happened.**
   ```bash
   make review
   python3 tools/review.py show <route_plan id>
   ```
   A plan with a capacity warning or an unscheduled high-priority ticket is
   `needs_human`; otherwise `pending_review`. Either way, nothing has been
   exported or posted to the team yet.

5. **Approve and dispatch.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
   "Dispatch" here means: write the route cards to
   `data/exports/routes-<date>.csv` (or a live Google Sheet, if configured -
   `docs/integrations.md`), and post the morning narrative to the staff
   channel. There is no specific "housekeeping app" this repo pushes to - see
   docs/how-it-works.md "Design decisions".

6. **Keep it running.**
   ```bash
   make watch                       # loop on the configured interval
   ```
   Or schedule it - `make schedule` and `scheduler/` have cron, launchd and
   systemd examples. `config/agent.yaml`'s `schedule.main` is the interval
   this repo ships with (every 30 minutes).

## Edge cases

- **A high-priority ticket's room is not on today's board at all.** It shows
  up as "unscheduled" in the plan and pushes the plan to `needs_human` - flag
  it to the supervisor directly, it will not silently disappear.
- **A ticket's room is not a guest room** (a public area like "Sauna Plant"
  or "Kitchen Walk-in"). It is routed straight to engineering with a note
  that housekeeping only needs the area closed - it is never forced into a
  cleaning route.
- **More routes than `housekeeping_headcount`.** The plan says so in plain
  language ("the overflow has to go to agency or the stayovers slip to
  tomorrow") rather than silently understaffing.
- **A re-run the same day.** `tools/routes.py optimise --day-offset 0` a
  second time the same calendar day returns the existing plan untouched -
  see docs/how-it-works.md "Idempotency".
