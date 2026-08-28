# Workflow: working the review queue

Objective: turn a queued route plan or triage run into a decision - approve,
edit, or reject - and, once approved, actually dispatch or apply it.

Nothing leaves this agent's own database without going through this. `mode:
shadow` is an unconditional kill switch - it blocks every dispatch/apply,
approved or not. Approving or editing here only records the decision; see
docs/safety.md for the full guard.

## Steps

1. **See what is waiting.**
   ```bash
   make review
   ```
   Each line shows the item id, its status (`pending_review` or
   `needs_human`), its kind (`route_plan` or `triage_run`), and the date.

2. **Read one in full.**
   ```bash
   python3 tools/review.py show <id>
   ```
   For a `route_plan`: the routes, the wing-vs-flat comparison, VIP flags,
   maintenance interleaves, any capacity warning, and the narrative note. For
   a `triage_run`: every ticket's trade, assignee, schedule, and any
   escalation reason. Summarise it for the hotel in plain language - do not
   paste the raw JSON at them.

3. **Decide.**
   ```bash
   python3 tools/review.py approve <id>
   python3 tools/review.py edit <id> --note-file my-note.txt [--headline "..."]
   python3 tools/review.py reject <id> --reason "wrong tone"
   ```
   `edit` only rewrites the narrative note - the routes, the trade
   assignments and the schedule are deterministic output and are not meant
   to be hand-edited (docs/how-it-works.md). If a route or a ticket's
   assignment looks wrong, that is a config or a fixture problem to fix
   (`config/agent.yaml`, `data/imports/`), not something to patch in the
   queue.

4. **Dispatch/apply what was approved.**
   ```bash
   python3 tools/review.py send
   ```
   This claims everything `approved`/`edited` and hands each item to its own
   dispatcher: a `route_plan` gets exported to a sheet and posted to
   housekeeping; a `triage_run` writes every ticket's final assignment and
   posts to engineering. In `mode: shadow` this always prints one readable
   `blocked ...` line per item and writes nothing - approved or not, there
   is no case shadow mode lets through (`core/review.py:evaluate_write`).
   `send` only really dispatches/applies once `mode: live` (see
   `workflows/90-go-live.md`).

5. **A failed dispatch/apply.** `send` marks the item `failed` with the error
   attached.
   ```bash
   python3 tools/review.py retry <id>
   ```
   re-queues it after you have fixed the cause (usually a mailbox/sheet
   credential - `make doctor` will say which).

## Rules

- Only `tools/review.py` writes `approved` / `edited` / `rejected`.
- Confirm with the hotel before dispatching/applying anything, even an
  approved item, the first few times. `workflows/90-go-live.md` covers when
  to stop doing that.
- Fast-forwarding a ticket to `done` (`python3 tools/triage.py complete <id>`)
  and issuing/revoking a key (`python3 tools/locks.py`) are both **not** part
  of this queue - see `workflows/11-triage.md` and `workflows/12-locks.md`.
- Before flipping to `live`, run `python3 tools/review.py stale` - it moves
  everything still `pending_review`/`needs_human`/`approved`/`edited` to
  `stale`, so nothing built up while `mode: shadow` was on goes out on the
  first live pass by surprise. See `workflows/90-go-live.md`.
