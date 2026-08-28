# Guardrails and safety

This agent touches your room board, your maintenance tickets and (through
guest email) something a guest wrote you. Everything below is built in, not
optional, and this page explains what it does and what is left for you to
decide.

## The two modes

| Mode | What happens |
|---|---|
| `shadow` (default) | The agent reads, thinks, drafts and queues. It **never** exports a route, posts to the staff channel, or writes a ticket's final assignee. |
| `live` | A route plan or triage run you approved is really dispatched/applied. Everything else still waits. |

`mode` lives in `config/hotel.yaml`. It is a global kill switch: flipping it
back to `shadow` stops every outbound action immediately, mid-schedule, with
no other change. `config/agent.yaml` can be stricter than `hotel.yaml`, never
looser.

Two more brakes:

- `make run ARGS="--dry-run"` computes everything and writes nothing, even in
  live mode.
- `review.require_approval_for` in `config/hotel.yaml` lists the actions that
  need a human even in live mode. The defaults (`send_email`, `send_message`,
  `pms_write`, `payment`, `publish`) already cover this agent's two dispatch
  actions (`send_message` via `notify_staff`, and the sheet export).

Every outbound action goes through one function,
`core/review.py:assert_write_allowed`. There is no second path.

## The review queue

Nothing is dispatched or applied without passing through the queue.

```bash
make review                             # what is waiting
python3 tools/review.py show <id>        # the full plan and how it got there
python3 tools/review.py approve <id>
python3 tools/review.py edit <id> --note-file my-note.txt
python3 tools/review.py reject <id> --reason "wrong tone"
python3 tools/review.py send             # dispatch/apply everything approved
```

Two kinds of item live here: `route_plan` (today's cleaning routes) and
`triage_run` (today's ticket triage). `edit` only rewrites the narrative note
- the routes, the trade assignments and the schedule underneath are
deterministic output, not something to hand-edit (docs/how-it-works.md
explains why).

## What The Steward will never do

- **Dispatch a route or apply a triage plan while `mode: shadow`**, or
  without an approval in `live` mode.
- **Skip a safety escalation.** A passport locked in a dead safe, a same-day
  inspection, a trip/slip hazard, HACCP/walk-in language, a confirmed VIP
  arrival, and a pilot/day-sleeper's jammed blackout blind are all
  hard-coded to force `priority: high` - there is no config knob that turns
  any of these off.
- **Turn off the VIP flag.** Every VIP room in today's board is called out
  by number for a supervisor re-check, independent of the five rule toggles
  (`config/agent.yaml: rules`) - this cannot be disabled.
- **Book a contractor-cost job without a paper trail.** With
  `contractor_threshold` on (default), a contractor-only trade or an
  estimate over `contractor_signoff_threshold` is held for the chief
  engineer's sign-off, never booked directly. Turning the rule off still
  shows the trade-off in plain text ("no second pair of eyes") rather than
  hiding it.
- **Absorb overflow silently.** More routes than
  `config/agent.yaml: housekeeping_headcount` models triggers an explicit
  warning ("goes to agency or slips to tomorrow") instead of quietly
  understaffing the day.
- **Replace the inspector's sign-off.** The roster is explicit about this.
  Nothing in this repo marks a room sellable - that checkpoint, wherever your
  property puts it, stays with a person. See docs/benefits.md.
- **Invent a maintenance ticket that is not there.** `tools/ticket_intake.py`
  only logs a ticket when the model is confident a guest described something
  broken; below `ticket_detect_confidence_threshold` the email is queued for
  a human instead of guessed into a ticket.
- **Call a real electronic lock system.** `tools/locks.py` writes only to
  this agent's own audit table (`hk_lock_events`) - see docs/integrations.md
  if you want to connect a real one.

## Data handling

**What leaves your machine.** With `llm.provider: anthropic` or
`claude-code`, three kinds of prompt go to Anthropic: a route-plan narrative,
a triage-run narrative, and a guest email being checked for a maintenance
issue. None of these carry payment data. With `llm.provider: mock` or
`interactive`, nothing leaves the machine at all.

**What is stored, and where.** Everything lives in `data/` inside this
folder: `agent.db` (SQLite, including the `hk_tickets` and `hk_lock_events`
tables this agent adds), `logs/*.jsonl`, `exports/`. `data/` is gitignored.
There is no cloud service behind this repo and no telemetry.

**Card numbers are redacted on the way in.** Guest email passes through
`core/redact.py` before it is stored, logged or shown to a model - the same
guarantee every repo in this family makes, even though a maintenance email is
unlikely to carry one.

**Retention.** `privacy.retention_days` (default 365) is how long processed
items stay in `data/agent.db`. Deleting the file deletes everything the agent
knows, including the ticket book.

## AI disclosure

This agent does not send anything to a guest - every output (the morning
route note, the triage note, a ticket) is read by housekeeping and
engineering staff, not by a guest. The EU AI Act Article 50 guest-disclosure
line this family's other repos carry on outbound guest email does not apply
here for that reason. If you extend `tools/ticket_intake.py` to also reply to
the guest who reported the issue (confirming "thanks, we have logged it"),
add that line to the reply then - see any front-desk-style repo's
`docs/safety.md` for suggested wording.

## Subscription or API: an honest note

Two ways to pay for the reasoning behind the route-note, triage-note and
ticket-detect prompts:

**Your Claude Code subscription** (`llm.provider: claude-code` or
`interactive`). Flat monthly cost. A property running The Steward every 30
minutes is a handful of short calls a day - well inside normal interactive
use. Automated use of a personal Pro/Max subscription is still subject to
Anthropic's usage policy and rate limits; read the terms and decide for
yourself.

**The Anthropic API** (`llm.provider: anthropic`). Pay per token, proper rate
limits, usage you can attribute. The right answer once you are running this
on more than one property or want the shortest possible interval.

Start on the subscription while you are learning what the agent does.

## If something goes wrong

1. `mode: shadow` in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env`.
   Every dispatch/apply stops on the next pass.
2. Remove the schedule (`crontab -e`, `launchctl unload`, or
   `systemctl disable --now <slug>.timer`).
3. `make doctor` to see what the agent thinks its state is.
4. `data/logs/*.jsonl` has every decision, with the run id, in order.
5. `python3 tools/review.py show <id>` has the full event trail for one plan
   or run; `python3 tools/triage.py list` has the ticket book.
