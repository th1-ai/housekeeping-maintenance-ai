# Trades and parts - Hotel Aurora (fixture data)

<!--
Copy this to knowledge/trades-and-parts.md and replace it with your own
engineers, contractors and parts notes. This file is for your own reference
and for briefing anyone who reads the triage output - tools/engine.py's
trade table does not read this file; it uses config/agent.yaml (engineer
names and thresholds) and its own fixed trade-routing rules.
-->

## Engineers

| Config key | Name | Trades |
|---|---|---|
| `engineers.mechanical` | Nuno | Plumbing, HVAC, carpentry, general maintenance |
| `engineers.electrical` | Marta | Electrical, electronics, in-room safes, AV, doorbells |

Update `config/agent.yaml: engineers.mechanical` / `engineers.electrical` to
match your own team. `tools/engine.py` never hardcodes a name - only these
two config keys. **One technician covering every trade?** Set both keys to
the same name - `tools/triage.py:_engineer_name` and
`tools/doctor.py:check_engineers` just look up whatever string is there, so
a single name works exactly like two.

## Contractors

| Trade | Contractor | Contact | Notes |
|---|---|---|---|
| Commercial refrigeration | ColdTech Services | +1 555 0199 | The only contractor-only trade by default - always held for sign-off over the threshold (`config/agent.yaml: contractor_signoff_threshold`, default 300). |

Add a row here for any other trade you route to a contractor rather than an
in-house engineer.

## Parts notes, by trade

These are the fixed `parts_note` strings `tools/engine.py`'s trade table
returns - useful context when you read a triage run, not something the code
reads from this file:

| Trade | Typical parts note |
|---|---|
| Commercial refrigeration | Not a stock item |
| Plumbing | Basin cartridge |
| Electrical | LED driver |
| HVAC | Capacitor and filter kit |
| Blinds and curtains | 2-day supplier lead, not a stock item |
| Gym equipment | Drive belt |

If your property keeps a real parts inventory, this is the first place a
future build should read from instead - see docs/how-it-works.md "Design
decisions".
