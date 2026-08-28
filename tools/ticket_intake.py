"""tools/ticket_intake.py - turn a guest email into a maintenance ticket.

The roster promise ("logs maintenance tickets from guest messages") has no
demo mechanism to port - the spec's 14 seeded tickets are all created by
hand (see docs/how-it-works.md "Design decisions"). This module is this
template's own answer: one LLM call per unread email
(`prompts/ticket-detect.md`) decides whether the guest is describing
something broken, and a plain confidence rule decides what happens next.

Creating a ticket is not a guarded write - it only appends to this agent's
own `hk_tickets` table, the same trust level as seeding it from fixtures. The
guarded, human-reviewed step is later, when triage assigns an engineer and a
schedule (`tools/triage.py`) and when that plan is applied or a route is
dispatched (`tools/review.py send`).
"""

from __future__ import annotations

import json
from pathlib import Path

from core.adapters.base import EmailMessage
from core.config import Settings
from core.llm import LLMResult, LLMSchemaError, complete
from core.store import Item, Store
from core.templates import build_prompt

import store_ext

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "schemas"
TICKET_DETECT_SCHEMA = json.loads((SCHEMAS_DIR / "ticket-detect.json").read_text(encoding="utf-8"))


def email_to_dict(msg: EmailMessage) -> dict:
    return {"id": msg.id, "from": msg.from_email, "from_name": msg.from_name,
           "subject": msg.subject, "body": msg.body_text, "received_at": msg.received_at}


def process_email(settings: Settings, store: Store, msg: EmailMessage, *,
                  provider: str | None = None) -> tuple[Item, bool, store_ext.TicketRow | None]:
    """Classify one email and, if it is a confident maintenance report, log a
    ticket. Returns ``(item, did_work, ticket_or_none)``.

    Idempotent: an email already checked (``item.intent`` set) is left alone.
    A model answer that does not match its schema is queued ``needs_human``
    rather than guessed - the same pattern as every prompt in this family.
    """
    item = store.upsert_item("email", msg.id, kind="ticket_intake", payload=email_to_dict(msg))
    if item.intent:
        return item, False, None

    prompt = build_prompt("ticket-detect", settings=settings, item=email_to_dict(msg),
                          fixture_id=msg.id)
    try:
        result: LLMResult = complete("ticket-detect", prompt, TICKET_DETECT_SCHEMA,
                                     settings=settings, provider=provider, store=store,
                                     item_id=item.id, fixture_id=msg.id)
    except LLMSchemaError as exc:
        store.set_fields(item.id, error=str(exc))
        updated = store.transition(item.id, "needs_human", actor="agent",
                                   detail={"error": "ticket_detect_schema_error"})
        return updated, True, None

    data = result.data or {}
    confidence = float(data.get("confidence", 0.0))
    store.set_fields(item.id, intent="checked", confidence=confidence,
                     payload={**email_to_dict(msg), "detection": data})

    if not data.get("is_maintenance_issue"):
        updated = store.transition(item.id, "skipped", actor="agent",
                                   detail={"reason": "not a maintenance issue"})
        return updated, True, None

    threshold = float(settings.agent_get("ticket_detect_confidence_threshold", 0.6))
    if confidence < threshold:
        updated = store.transition(item.id, "needs_human", actor="agent",
                                   detail={"reason": "low-confidence maintenance detection",
                                          "confidence": confidence})
        return updated, True, None

    room = str(data.get("room") or "").strip() or "unspecified"
    summary = str(data.get("summary") or msg.subject or "Guest-reported issue")
    detail = str(data.get("detail") or msg.body_text or "")
    ticket = store_ext.create_ticket(store, room=room, summary=summary, detail=detail,
                                     priority="medium", source="guest_email", item_id=item.id)
    store.transition(item.id, "dispatched", actor="agent", detail={"ticket_id": ticket.id})
    updated = store.transition(item.id, "auto_sent", actor="agent",
                               detail={"ticket_id": ticket.id, "room": room})
    return updated, True, ticket
