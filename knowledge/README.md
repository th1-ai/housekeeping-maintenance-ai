# knowledge/

This folder is the agent's memory of your property. It reads these files before
it answers anything, so the quality of what is in here is the quality of what
goes out.

## What to put here

| File | What it holds |
|---|---|
| `property.md` | The facts. Rooms, floors, room types, policies - what the morning route note and the triage note refer to. |
| `faq.md` | Not used by this agent's prompts today. Keep it if another repo in this family shares the property. |
| `trades-and-parts.md` | Your real engineers' names, your contractor list, and what "not a stock item" actually means for each trade - see `tools/engine.py`'s trade table. |

Copy the `.example.md` files, rename them without `.example`, and fill them in:

```bash
cp knowledge/property.example.md knowledge/property.md
cp knowledge/trades-and-parts.example.md knowledge/trades-and-parts.md
```

`knowledge/*.md` is gitignored (the `.example.md` files are not), because your
property notes are yours.

## How to write it

**Write it the way you would brief a new floor supervisor.** Short sentences,
concrete facts, no marketing language. The morning route note and the triage
note both draw on this, so anything vague here becomes a vague note.

**Be specific about numbers and times.** "Check-in from 15:00" is usable.
"Check-in in the afternoon" is not.

**Say what you do NOT do.** "We have no parking; the nearest car park is X, about
EUR 15 a day" prevents a wrong answer far better than silence does.

**Keep prices dated.** "Breakfast EUR 18 per person (2026 rates)" tells the agent
and you when it is stale.

**One fact per line where you can.** It makes the agent's job easier and it makes
your job easier when something changes.

## Keeping it current

The agent is only as right as this folder. When a policy changes, change it here
first. A good habit: whenever you correct one of the agent's drafts in the review
queue, ask whether the correction belongs in `property.md`. If it does, the agent
stops making that mistake.

You can also ask your Claude Code session to do it:

> Read knowledge/property.md and the last ten items in the review queue. If any
> of my edits contradict what is in the file, tell me which line to change.
