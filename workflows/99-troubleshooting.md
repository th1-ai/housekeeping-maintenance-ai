# Workflow: troubleshooting

Read the whole error before doing anything - every tool here is written to
say what broke and what to do about it. If you fix something not covered
below, add it here.

## `make doctor` shows a FAIL

Each `FAIL` line has a `->` fix hint right under it. Common ones:

- **`hotel identity`: name is still 'Hotel Aurora'.** Expected on a fresh
  clone. Edit `config/hotel.yaml`.
- **`hk rules`: missing toggle(s).** Copy `config/agent.example.yaml` to
  `config/agent.yaml` - it ships with all five rules.
- **`engineers`: mechanical/electrical engineer names not set.** Set
  `config/agent.yaml: engineers.mechanical` / `engineers.electrical`.
- **`room board`: no rooms in data/imports/... or fixtures/hotel/...** See
  docs/integrations.md for the CSV columns, or restore the shipped fixture
  from git.
- **An adapter shows FAIL, not warn.** `universal`/`built` adapters fail loud
  when misconfigured (a `warn` is reserved for stubs). Read the `detail`
  column - it names the missing file or variable.

## `make demo` does not print `DEMO OK`

- Make sure `make setup` ran first (`.venv` must exist).
- `tools/demo.py` forces `llm.provider=mock` and reads
  `fixtures/hotel/*.json` and `fixtures/inbound/*.json` - if you deleted or
  renamed those files, restore them from git.
- Read the traceback if there is one; `tools/demo.py` does not swallow errors
  on purpose.

## `make run` exits with code 3

Not an error. `llm.provider: interactive` parked one or more prompts (a
`ticket-detect` prompt per new email that needs one, plus one `route-note`
and one `triage-note` once ticket intake has finished). If several emails
each need an answer, `tools/run.py` parks all of them in the same pass
before it stops - you do not need to re-run once per email. Read every
`data/pending/*.prompt.md`, write each answer to the matching
`*.answer.json` (JSON only, matching the schema shown), and run the same
command again.

## `make run` crashes with `AdapterNotConfigured` (e.g. `systems.email.adapter: imap`)

Fixed: this is caught and printed as one readable
`integration error: ...` line plus "Run `make doctor` ...", not a
traceback. If you see a raw traceback instead, you are running an older
copy of `tools/run.py` - update from git. `make doctor` names exactly which
`.env` variable is missing (`IMAP_HOST`, `EMAIL_ADDRESS`, ...).

## A `config/*.yaml` file has bad indentation or a stray colon

Every tool prints one readable `config error: <file> (line N, column M):
<what YAML complained about>. Check the indentation (two spaces, no
tabs)...` line, not a traceback (`core/config.py:load_yaml`). Compare the
line it names against the matching `.example.yaml`.

## A ticket's trade looks wrong

`tools/engine.py:trade_for()` is a first-match regex table - a summary that
happens to contain a word from an earlier rule (for example, "not safe to
use" containing the word "safe") can match the wrong trade. Check
`tools/engine.py`'s `_TRADE_RULES` list and either reword the ticket's
summary/detail or tighten the pattern. This is exactly the kind of thing
`tests/test_housekeeping_engine.py` is there to catch before it reaches a
real property - add a test alongside your fix. Each rule already carries
English, Spanish, French, German, Italian and Portuguese keywords for the
same fault - if a ticket in another language still falls through to
"general maintenance", add that language's keyword to the matching rule
(and a test) rather than translating the ticket text.

## An escalation fired that should not have, or did not fire when it should

`escalation_for()` checks a fixed set of safety/VIP patterns in order and
stops at the first match. Read `docs/how-it-works.md`'s "Trade routing and
escalation" section for the exact order, then check the ticket's actual
`summary`/`detail` text against the pattern. The VIP-arrival rule only fires
when the named guest is a confirmed VIP in the PMS (`tools/triage.py:vip_names()`)
- a name in the ticket text alone is not enough.

## An item is stuck at `sending`

A process died between claiming an item and finishing the dispatch/apply.
`tools/run.py` calls `core.store.Store.reap_stuck_sending()` on every pass,
which moves anything stuck for more than 30 minutes to `failed` so you see it
in the queue instead of it vanishing. Use
`python3 tools/review.py retry <id>` once the cause is fixed.

## `python3 tools/*.py` says `command not found: python`, or `ModuleNotFoundError: No module named 'core'`

Same root cause either way: you ran it with a Python that is not the
repo's virtualenv (or there is no plain `python` on `PATH` at all - some
systems only ship `python3`), or from outside the repo root. The `make`
targets always call `.venv/bin/python` for you, so prefer those. Running a
`tools/*.py` script directly needs one extra step first:

```bash
source .venv/bin/activate        # once per terminal session, then `python` is correct
# or, without activating:
.venv/bin/python tools/run.py --once
```

## Still stuck

`data/logs/*.jsonl` has every decision the agent made, in order, with a run
id. `python3 tools/review.py show <id>` has the full event trail for one plan
or run. `python3 tools/triage.py list` has the current ticket book. If none of
that explains it, that is a real bug - describe exactly what you ran and what
you expected, and ask.
