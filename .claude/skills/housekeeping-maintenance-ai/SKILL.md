---
name: housekeeping-maintenance-ai
description: Run Housekeeping & Maintenance AI ("The Steward") — Builds the optimal cleaning route from arrivals/departures/stayovers, predicts linen needs, logs maintenance tickets from guest messages, and triages them with context — a VIP arriving in five days bumps that room's repair up the list.. Use when the user asks to run the agent, check what is waiting for review, approve or reject a draft, or asks how the agent is doing. Trigger phrases: "run The Steward", "/housekeeping-maintenance-ai", "check the queue", "what is waiting for me", "approve that draft".
---

# Housekeeping & Maintenance AI

Runs Housekeeping & Maintenance AI and works its review queue. Everything happens from the repo
root; every command below exists and works.

## Before anything else

Read `README.md` if you have not this session, and `workflows/10-routes.md` +
`workflows/11-triage.md` for the main loop. If the user has never run this
agent, start at `workflows/00-setup.md` instead and walk them through it.

## The loop

**1. Check the agent is healthy.**

```bash
make doctor
```

Any `FAIL` line has a fix hint. Fix it before going further. `WARN` lines are
worth mentioning but do not stop the run.

**2. Run one pass.**

```bash
make run                        # checks email, triages tickets, builds routes
make run ARGS="--limit 5"       # just the first five new emails
make run ARGS="--dry-run"       # compute everything, write nothing
```

If `llm.provider` is `interactive`, the run will stop with exit code 3 and park
prompts in `data/pending/`. That is expected - there can be several in one
pass (one per new email, one route-note, one triage-note). Read each
`*.prompt.md`, write your answer as JSON to the matching `*.answer.json`
following the schema exactly, then run the same command again.

**3. Show what is waiting.**

```bash
make review
python3 tools/review.py show <id>
```

Summarise it for the user in plain language: for a route plan, how many
rooms and where the pressure sits; for a triage run, what was escalated and
why, and what needs a contractor. Do not paste raw JSON at them.

**4. Act on their decision.**

```bash
python3 tools/review.py approve <id>
python3 tools/review.py edit <id> --note-file <path>
python3 tools/review.py reject <id> --reason "<why>"
```

`edit` only rewrites the narrative note - the routes and the trade/schedule
decisions are deterministic and are not meant to be hand-edited
(`docs/how-it-works.md`). Once approved:

```bash
python3 tools/review.py send     # dispatches routes / applies triage
```

**5. Two things are not part of this queue** - they are direct actions a
person runs deliberately, see `workflows/11-triage.md` and
`workflows/12-locks.md`:

```bash
python3 tools/triage.py complete <ticket_id>   # fast-forward: job is done
python3 tools/locks.py issue --room <r> --detail "<why>"
python3 tools/locks.py revoke --room <r> --detail "<why>"
```

**6. Report.**

```bash
make report
```

## Rules

- **Never dispatch or apply in shadow mode**, and never work around a
  blocked write. The error message says what to do.
- **Going live is the hotel's decision.** Only raise it after
  `workflows/90-go-live.md` has been worked through.
- **The five safety/VIP escalation rules and the VIP flag never turn off** -
  see docs/safety.md.
- **This agent never replaces the inspector's sign-off** - do not imply a
  route or a ticket completing makes a room sellable.
- **Never print or paste a credential.**
- If a run fails, read the whole error, fix the cause, re-run, and note what
  you learned in `workflows/99-troubleshooting.md`.
