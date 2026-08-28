# Workflow: shadow to live

Objective: decide, together with the hotel, whether The Steward is ready to
dispatch approved routes and apply approved triage runs on its own instead of
only queuing them - and make the change safely if so.

This is the hotel's decision, never the agent's. Do not suggest it until the
checklist below is genuinely true, and when you do raise it, say plainly what
changes.

## Checklist

- [ ] `make doctor` is clean (no `FAIL` lines). `warn` on `mode` is expected
      until you flip it.
- [ ] `config/hotel.yaml` has the real property, and
      `knowledge/property.md` + `knowledge/trades-and-parts.md` exist and are
      accurate (not the shipped examples).
- [ ] `config/agent.yaml`'s `housekeeping_headcount`, `engineers`, and
      `contractor_signoff_threshold` reflect this property, not the fixture
      defaults.
- [ ] The room board and ticket list come from `data/imports/*.csv` (your own
      PMS/CMMS export), not `fixtures/hotel/`.
- [ ] At least a few days of real `make run` passes have gone through the
      review queue, not just the demo fixtures - and the hotel trusts the
      trade routing, the escalation reasons, and the wing/flat routing
      numbers it has seen.
- [ ] The hotel has decided which dispatch channel is real:
      `systems.sheets.adapter` and `systems.messaging.adapter` are both
      pointed at something a real person reads (`docs/integrations.md`), not
      `mock` or `csv`-into-nowhere.
- [ ] `python3 tools/review.py stale` has been run, so the shadow-era queue
      cannot go out by surprise on the first live pass (step 1 below).

## What never changes

- The five safety/VIP escalation rules always fire - there is no live-mode
  setting that turns any of them off.
- The VIP flag on the route plan always shows.
- A contractor-cost job is held for sign-off whenever `contractor_threshold`
  is on, in shadow or live.
- Nothing here ever marks a room sellable or replaces the inspector's
  sign-off (docs/benefits.md, docs/safety.md).

## Making the change

1. **While still in shadow**, run `python3 tools/review.py stale`. It moves
   anything still `pending_review`/`needs_human`/`approved`/`edited` from
   the shadow era to `stale`, so nothing built up before you trusted the
   agent goes out on the very first live pass. Do this before step 2, not
   after - once `mode: live` is set, an already-approved item is one
   `python3 tools/review.py send` away from really dispatching. A `stale`
   item can be revived by hand if it still matters (`core/store.py` allows
   `stale -> pending_review`).
2. Edit `config/hotel.yaml`:
   ```yaml
   mode: live
   ```
3. `review.require_approval_for` still lists `send_message` by default - it
   should. Going live means **approved plans get dispatched/applied**, not
   that The Steward starts acting on unapproved ones.
4. Run `make doctor` again to confirm.
5. Run one real pass and manually watch a dispatch go through:
   ```bash
   make run ARGS="--limit 1"
   python3 tools/review.py list
   python3 tools/review.py approve <id>
   python3 tools/review.py send
   ```
6. Tell the hotel exactly what just changed: an approved route plan or
   triage run now really reaches the sheet and the staff channel the next
   time someone (or a scheduled job) runs `python3 tools/review.py send` - it
   is still never automatic before that approval.

## Going back to shadow

```yaml
mode: shadow
```
in `config/hotel.yaml`, or `AGENT_MODE=shadow` in `.env` for one run. Either
stops every dispatch/apply on the next pass, mid-schedule, with no other
change required.
