# Connecting your systems

Every connector in this repo is one of three things, and the table says which.
We will not tell you an integration exists when it does not.

| Badge | Means |
|---|---|
| **built** | Written against the real API and tested against it. |
| **universal** | Works with any system through a common protocol: IMAP/SMTP, CSV, a webhook. |
| **stub** | Interface only. Calling it raises a clear error with a recipe for adding it. |

Check what is actually working on your machine at any time:

```bash
make doctor
```

The Steward uses four of the family's adapters (PMS for VIP context only,
email to catch maintenance issues in guest messages, sheets and messaging to
dispatch the finished plan) plus one stub (locks). It does not use `pos`,
`accounting`, `reviews`, `calendar`, `payments`, `procurement` or `courier` at
all.

## The room board and the ticket list

These are not one of the family's four adapter systems - there is no
standard "housekeeping board" or "CMMS" interface in `core/adapters/base.py`,
so this template reads them directly:

| Source | Used for |
|---|---|
| `data/imports/room_status.csv` (if present), else `fixtures/hotel/room_status.json` | Today's room board: `room_number, floor, room_type, status, vip, note`. `status` is one of `checkout`, `turn`, `stayover`, `arrival`, `vacant`. |
| `data/imports/maintenance_tickets.csv` (if present), else `fixtures/hotel/maintenance_tickets.json` | Tickets to seed once: `id, room, summary, detail, priority`. After that, tickets live in this agent's own `hk_tickets` table (`tools/store_ext.py`), which `make doctor`, `tools/report.py` and `tools/triage.py list` all read. |

Export both CSVs from your PMS/CMMS on whatever cadence suits you (nightly is
plenty) and drop them in `data/imports/`. `tools/store_ext.py:load_room_board`
and `load_seed_tickets` prefer the CSV the moment it exists, so there is
nothing to switch in config. See `#implement-your-own` below if you would
rather write a small script that pulls this straight from your PMS's API.

## Status

### PMS - `systems.pms.adapter`

Read-only in this agent: `tools/triage.py:vip_names()` calls
`pms.list_reservations()` once per pass to build the set of confirmed VIP
guests the escalation rule checks against. Nothing here reads availability or
rates, and nothing writes to the PMS.

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/hotel/reservations.json`. What `make demo` uses; each fixture reservation carries its guest's `vip` flag directly. |
| `csv` | universal | two CSV exports | Reads `data/imports/reservations.csv` for the stay dates and the guest's name/email, **and** `data/imports/guests.csv` for the `vip` column - see below. |
| `cloudbeds` | built | OAuth app + refresh token | Live reads. Only worth connecting if another agent on the same property already needs it. |

**Why two files for `csv`.** `reservations.csv` has no `vip` column of its
own to read - `vip` only lives on a guest record, in `guests.csv`.
`vip_names()` resolves each reservation's guest by email (falling back to
name) against `guests.csv` and trusts that record's `vip` flag. Export
both files - a reservation with no matching row in `guests.csv` is treated
as not VIP, the same as a hotel with no PMS connection at all.

A missing or broken PMS does not stop a run - `vip_names()` catches the
error and returns an empty set, so the VIP-arrival escalation rule simply
does not fire rather than crashing.

### Email - `systems.email.adapter`

Used by `tools/ticket_intake.py` to catch a maintenance issue hiding in a
guest email. The Steward never replies to the guest; it only reads.

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/inbound/*.json`. |
| `imap` | universal | mailbox + app password | Any provider. **Start here.** |
| `gmail` | built | Google OAuth desktop client | Adds Gmail labels and threads. |

```
EMAIL_ADDRESS=reservations@example.com
EMAIL_PASSWORD=            # an APP password, never your login password
IMAP_HOST=imap.example.com
```

### Messaging - `systems.messaging.adapter`

`tools/routes.py:dispatch_route_plan` and `tools/triage.py:apply_triage_run`
both call `notify_staff()` with the finished narrative, once a human has
approved it.

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Writes to `data/exports/sent_messages.jsonl`. |
| `unipile` | built | your own UniPile account | WhatsApp on your own number. |
| `webhook` | universal | any URL | POST to Zapier, Make, n8n, or your own housekeeping/engineering chat tool. |

### Sheets - `systems.sheets.adapter`

`tools/routes.py:dispatch_route_plan` writes one row per route card here -
this is "dispatch to the housekeeping app" in this template (see
docs/how-it-works.md "Design decisions").

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `csv` | universal | nothing | Writes `data/exports/routes-<date>.csv`. |
| `google` | built | service account JSON | A live shared spreadsheet the floor supervisors can open on their phones. |

### Locks - stub

`tools/locks.py` writes only to this agent's own `hk_lock_events` audit
table - it never calls a real electronic lock system, matching the source
spec exactly ("no real lock/PMS-key-system call"). `core/adapters/base.py`
already defines the shape (`Locks.issue_key`, guarded `locks_write`) if you
want to connect one.

### Everything else

`pos`, `accounting`, `reviews`, `calendar`, `payments`, `procurement` and
`courier` are **stubs** this agent does not call at all.

## Implement your own

<a id="implement-your-own"></a>

Open `claude` in this folder and paste:

> Read `docs/integrations.md#implement-your-own` and `core/adapters/base.py`.
> I want The Steward to read the room board and maintenance tickets straight
> from **<your PMS/CMMS>** instead of `data/imports/*.csv`. Its API docs are
> at **<url>** and I have credentials in `.env` as `<VAR names>`. Write a
> small script in `tools/` that pulls both lists and writes them to
> `data/imports/room_status.csv` and `data/imports/maintenance_tickets.csv`
> in the columns `tools/store_ext.py` already expects, then wire it into
> `make run` (or its own cron entry) ahead of `tools/run.py`.

To connect a real electronic lock system instead of the local audit log:
copy `core/adapters/pms_csv.py` as the shape for a new `core/adapters/locks_*.py`,
implement `ping()`, `capabilities()` and `issue_key()` against
`core/adapters/base.py:Locks`, register it in `core/adapters/__init__.py`'s
`STUBS` table, and call it from `tools/locks.py:cmd_issue` behind the
existing `guarded_write("locks_write")` decorator so shadow mode still
applies to a real system.

### Rules that matter

- **`ping()` never raises.** It returns `HealthCheck(ok=False, ...)` with a
  hint. A broken adapter must still produce a readable doctor table.
- **Every write is decorated** with `@guarded_write("<action>")`. No
  exceptions - that is the whole safety model (`core/review.py`).
- **Never log a credential.** `core/log.py` masks anything whose key looks
  like a secret, but do not rely on it.
- **Redact on ingestion.** Guest email passes through `core.redact.redact()`
  automatically inside the email adapters before it is stored or shown to a
  model.
- **Write a test.** Copy `tests/test_core_adapters_mock_csv.py` or
  `tests/test_housekeeping_flow.py` for the shape.

### `core/` is shared

`core/` is identical in all 28 agents in this family. A hotel-specific tweak
belongs in `tools/` or your own adapter file, never in `core/`.
