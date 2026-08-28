# Workflow: first-run setup

Objective: get The Steward from a fresh clone to a working demo, then to real
config, in one sitting.

## Steps

1. **Install and check.**
   ```bash
   make setup
   make doctor
   ```
   `make setup` creates the virtualenv, installs `requirements.txt`, and
   copies `.env.example` -> `.env` and every `config/*.example.yaml` ->
   `config/*.yaml` (only if those files do not exist yet). `make doctor` will
   show a `FAIL` on "hotel identity" right after setup - expected, the
   property name is still the shipped placeholder. Everything else should be
   `ok` or `warn`.

2. **Run the demo.** No credentials needed.
   ```bash
   make demo
   ```
   Expect to see four sample emails checked for a maintenance issue, every
   open ticket triaged, today's routes built with that triage folded in, and
   a Locksmith issue/revoke pair. The last line should read
   `DEMO OK — 2 items processed, 2 drafted, 0 sent (shadow)`. If you do not
   see that, stop and read `workflows/99-troubleshooting.md`.

3. **Fill in the property.** Edit `config/hotel.yaml` (name, address, contact,
   timezone, rooms). Then:
   ```bash
   cp knowledge/property.example.md knowledge/property.md
   cp knowledge/trades-and-parts.example.md knowledge/trades-and-parts.md
   ```
   Replace the Hotel Aurora content with your own property. See
   `knowledge/README.md`.

4. **Set your own rules.** Copy `config/agent.example.yaml` to
   `config/agent.yaml` if `make setup` has not already (it should have), then
   edit:
   - `housekeeping_headcount` - your real attendant count.
   - `engineers.mechanical` / `engineers.electrical` - your real engineers.
   - `contractor_signoff_threshold` - your own sign-off limit.
   - `rules` - all five default on; turn one off only once you understand
     what changes (docs/how-it-works.md has the exact before/after for each).

5. **Point at your real room board and ticket list.** `docs/integrations.md`
   covers the CSV columns for `data/imports/room_status.csv` and
   `data/imports/maintenance_tickets.csv` - export these from your PMS/CMMS
   on whatever schedule suits you. Until you do, The Steward runs on the
   bundled fixtures. Optional: export `data/imports/reservations.csv` and
   `data/imports/guests.csv` too if you want the VIP-arrival escalation rule
   to fire on your own property - `docs/integrations.md` "PMS" explains why
   it needs both files.

6. **Pick how the agent thinks.** `config/hotel.yaml`'s `llm.provider` starts
   as `interactive` - it asks you, in this Claude Code session, instead of
   calling a model. `docs/how-it-works.md` and `docs/safety.md` cover the
   other three providers (`mock`, `claude-code`, `anthropic`).

7. **Connect a real mailbox (optional).** This is only used to catch a
   maintenance issue hiding in a guest email - see
   `workflows/11-triage.md`. `systems.email.adapter` starts as `mock`.

8. **Re-check.**
   ```bash
   make doctor
   ```
   Once the property name is real, `knowledge/property.md` exists, and the
   room board is not the fixture, move on to `workflows/10-routes.md` and
   `workflows/11-triage.md`.
