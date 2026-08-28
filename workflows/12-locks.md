# Workflow: The Locksmith

Objective: keep an audit trail of who a key or access code was issued to, and
revoke it when the job is done.

This is a built-in feature of The Steward, not a separately-enabled
sub-agent - see `docs/sub-agents.md`. There is nothing to turn on.

## Steps

1. **Issue a key or code.**
   ```bash
   python3 tools/locks.py issue --room "214" --detail "Contractor sauna repair code"
   ```
   This is a direct action - run it because a person (you, or the hotel's
   Claude session on their behalf) decided to hand out access, not something
   the agent decides on its own. It is not gated by `mode: shadow` or the
   review queue; there is no draft to review first. It writes only to this
   agent's own audit table - no real electronic lock system is called (see
   docs/integrations.md if you want to connect one).

2. **Revoke it once the job is done.**
   ```bash
   python3 tools/locks.py revoke --room "214" --detail "Repair finished"
   ```

3. **Check the feed at any time.**
   ```bash
   python3 tools/locks.py feed
   python3 tools/locks.py feed --limit 10
   ```
   Newest first. Every issue and revoke you have ever run is here, with the
   room, the detail you gave, the actor, and the timestamp.

## Edge cases

- **A non-numeric "room"** (a plant room, a back-of-house area) works the
  same way - `--room` is a free-text label, not validated against the room
  board.
- **You want a real lock system, not just a log.** Copy
  `core/adapters/pms_csv.py` as the shape for a new
  `core/adapters/locks_*.py`, implement `Locks.issue_key()` against
  `core/adapters/base.py`, and wire it into `tools/locks.py:cmd_issue` behind
  the existing guard - see docs/integrations.md#implement-your-own.
