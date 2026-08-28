# Sub-agents in this repo

None. The agent roster this family of templates is built from lists no agent
whose parent is "Housekeeping & Maintenance AI" - this repo is single-agent.

## The Locksmith - a built-in feature, not a sub-agent

The source demo folds in a lock/key desk called "The Locksmith" alongside the
room board and maintenance triage on the same page. It has no card of its own
on that roster - a known quirk of how it was carried over - so it is not a
sub-agent in the sense the rest of this family uses the word: it has no
`subagents.<name>.enabled` toggle, no `workflows/2x-*.md` file, and it is
always on.

What it is: `tools/locks.py`, a small audit feed for "issue a key/code for a
room" and "revoke it" - see `workflows/12-locks.md`. Every action is a
direct, human-run command; there is nothing to enable and nothing to review,
because it never calls a real lock system (docs/integrations.md) and never
gates a room's sellability.

## Lost & Found AI - a neighbour, not a child

The source demo also shares this same page with a separately-rostered,
top-level agent, Lost & Found AI ("The Finder"). It has its own repo
(`lost-found-ai`) and its own engine function (`runClaimMatcher`) - it is not
folded into this one. If a hotel runs both, they simply happen to cover the
same physical desk in the source demo; nothing in either repo assumes the
other is installed.
