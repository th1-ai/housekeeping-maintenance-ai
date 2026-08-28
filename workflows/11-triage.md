# Workflow: maintenance triage

Objective: turn the ticket list into a trade-routed, scheduled plan, catch
anything hiding in guest email, and apply it once a person has looked it
over.

## Inputs

- Seeded tickets: `data/imports/maintenance_tickets.csv` if it exists, else
  `fixtures/hotel/maintenance_tickets.json`.
- Guest email (`systems.email.adapter`, `mock` by default).
- `config/agent.yaml`'s `contractor_signoff_threshold`, `engineers`,
  `engineer_start_hour` / `engineer_close_hour` / `travel_minutes_between_jobs`,
  and the `contractor_threshold` rule.

## Steps

1. **Check guest email for a maintenance issue.**
   ```bash
   make run                         # does this, then triages, then builds routes
   ```
   Each new email is checked once (`prompts/ticket-detect.md`, one model
   call). A confident "yes" logs a ticket straight away - this is not a
   guarded write, just an entry in this agent's own ticket table, the same
   trust level as a ticket seeded from a fixture. A confident "no" is
   skipped. Anything below `ticket_detect_confidence_threshold` is queued
   `needs_human` instead of guessed either way.

2. **Triage every open ticket.**
   ```bash
   python3 tools/triage.py run
   ```
   For each ticket: `tools/engine.py:trade_for()` picks a trade and an
   engineer, `escalation_for()` checks it against the fixed safety/VIP rules,
   and the contractor-threshold and scheduling rules decide when it happens.
   Read the thinking log - every escalation prints the room and the exact
   reason it jumped the queue.

3. **If `llm.provider` is `interactive`,** answer the parked prompts in
   `data/pending/` the same way as every other workflow in this family.

4. **See what is waiting.**
   ```bash
   make review
   python3 tools/review.py show <triage_run id>
   ```
   A run with any escalation or any contractor hold is `needs_human`;
   otherwise `pending_review`.

5. **Approve and apply.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
   "Apply" writes every ticket's final `status` (`triaged`), `assignee`,
   `trade`, `eta` and `parts_note`, then posts the triage narrative to the
   engineering channel.

6. **Fast-forward when a job is actually done.**
   ```bash
   python3 tools/triage.py complete <ticket_id>
   ```
   This is not gated by review - it records something that already happened
   in the real world (the engineer finished the job) and releases the room
   on the shared board. Only use it once the work is genuinely finished.

7. **List tickets any time.**
   ```bash
   python3 tools/triage.py list                  # every ticket
   python3 tools/triage.py list --status open     # not yet triaged
   python3 tools/triage.py list --status triaged  # scheduled, not yet done
   ```

## Edge cases

- **A ticket's trade cannot be matched.** It falls back to "general
  maintenance", assigned to the mechanical engineer, 45 minutes - never
  dropped.
- **Two safety rules could both apply.** `escalation_for()` checks them in a
  fixed order and stops at the first match (docs/how-it-works.md) - the
  reason you see is always the first one that fired, not necessarily every
  one that could have.
- **A VIP-arrival mention in a ticket that is not a real VIP.** The rule only
  fires when the named guest matches a confirmed VIP reservation
  (`systems.pms.adapter`) - a name alone in the ticket text is not enough.
- **The PMS is not configured or is unreachable.** VIP-arrival escalation
  quietly does not fire rather than failing the run - see
  docs/integrations.md.
- **A re-run the same day.** `python3 tools/triage.py run` a second time
  returns the existing triage run untouched.
