"""Structured lead extraction with review/accept workflow."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId

from app.services.ai_config import resolve_ai_settings
from app.services.ai_context import load_conversation_context
from app.services.ai_prompt import build_chat_messages
from app.services.ai_provider import chat_completion

ALLOWED_FIELDS = frozenset(
    {
        "customer_name",
        "email",
        "company",
        "location",
        "product_interest",
        "budget_min",
        "budget_max",
        "preferred_datetime",
        "urgency",
        "decision_timeframe",
        "booking_intent",
        "purchase_intent",
        "support_issue_category",
        "language",
    }
)

LEAD_FIELD_MAP = {
    "customer_name": "name",
    "email": "email",
    "company": "company",
    "location": "location",
    "product_interest": "product_interest",
    "budget_min": "budget_min",
    "budget_max": "budget_max",
    "preferred_datetime": "preferred_datetime",
    "urgency": "urgency",
    "decision_timeframe": "decision_timeframe",
    "booking_intent": "booking_intent",
    "purchase_intent": "purchase_intent",
    "support_issue_category": "support_issue_category",
    "language": "language",
}

FORBIDDEN_LEAD_UPDATES = frozenset(
    {
        "whatsapp_consent_status",
        "blacklisted",
        "password_hash",
        "user_id",
        "consent",
    }
)

_JSON_BLOCK = re.compile(r"\{[\s\S]*\}")


def _parse_extraction(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    m = _JSON_BLOCK.search(text)
    if not m:
        raise ValueError("No JSON object")
    data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("Expected object")
    fields = data.get("fields") if isinstance(data.get("fields"), dict) else data
    out: dict[str, Any] = {}
    for k, v in fields.items():
        key = str(k)
        if key not in ALLOWED_FIELDS:
            continue  # reject unknown by skipping
        if isinstance(v, dict):
            val = v.get("value")
            conf = float(v.get("confidence") or 0)
            src = v.get("source_message_ids") or []
        else:
            val, conf, src = v, 0.5, []
        if val is None or val == "":
            continue
        out[key] = {
            "value": val if not isinstance(val, str) else val[:200],
            "confidence": max(0.0, min(1.0, float(conf))),
            "source_message_ids": [str(x) for x in (src if isinstance(src, list) else [])][:5],
        }
    return out


def extract_suggestions_sync(db, *, tenant_id: str, lead_id: str) -> list[dict]:
    user = db.users.find_one({"_id": ObjectId(tenant_id)})
    ai = resolve_ai_settings(user)
    if not ai["enabled"] or not ai["extraction_enabled"]:
        return []

    ctx = load_conversation_context(db, tenant_id=tenant_id, lead_id=lead_id)
    if len(ctx["messages"]) < 2:
        return []

    system = (
        "Extract structured business fields from the conversation. "
        "Return ONLY JSON: {\"fields\": {\"field\": {\"value\": ..., \"confidence\": 0-1, "
        "\"source_message_ids\": []}}}. "
        f"Allowed fields: {', '.join(sorted(ALLOWED_FIELDS))}. "
        "Do not invent sensitive personal attributes (race, health, religion, politics). "
        "Omit unknown fields."
    )
    messages = build_chat_messages(system=system, context_messages=ctx["messages"][-12:])
    result = chat_completion(
        messages=messages,
        model=ai["model"],
        fallback_model=ai["fallback_model"],
        temperature=0.1,
        max_tokens=500,
        tenant_id=tenant_id,
        operation="extraction",
        conversation_id=lead_id,
        response_format={"type": "json_object"},
    )
    if not result.success:
        return []
    try:
        parsed = _parse_extraction(result.text)
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    created = []
    for field, meta in parsed.items():
        if float(meta["confidence"]) < 0.4:
            continue
        doc = {
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "field": field,
            "suggested_value": meta["value"],
            "confidence": meta["confidence"],
            "source_message_ids": meta["source_message_ids"],
            "status": "pending",
            "model": result.model,
            "extracted_at": now,
            "created_at": now,
            "updated_at": now,
        }
        # Dedupe pending same field+value
        existing = db.ai_suggestions.find_one(
            {
                "tenant_id": tenant_id,
                "lead_id": lead_id,
                "field": field,
                "suggested_value": meta["value"],
                "status": "pending",
            }
        )
        if existing:
            continue
        res = db.ai_suggestions.insert_one(doc)
        doc["_id"] = res.inserted_id
        created.append(doc)
    return created


def accept_suggestion(
    db,
    *,
    tenant_id: str,
    lead_id: str,
    suggestion_id: str,
    actor_id: str,
) -> Optional[dict]:
    if not ObjectId.is_valid(suggestion_id):
        return None
    sug = db.ai_suggestions.find_one(
        {
            "_id": ObjectId(suggestion_id),
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "status": "pending",
        }
    )
    if not sug:
        return None
    field = sug.get("field")
    lead_field = LEAD_FIELD_MAP.get(field)
    if not lead_field or lead_field in FORBIDDEN_LEAD_UPDATES:
        return None
    if float(sug.get("confidence") or 0) < 0.4:
        return None
    value = sug.get("suggested_value")
    db.leads.update_one(
        {"_id": ObjectId(lead_id), "user_id": tenant_id},
        {"$set": {lead_field: value, "updated_at": datetime.now(timezone.utc)}},
    )
    db.ai_suggestions.update_one(
        {"_id": sug["_id"]},
        {
            "$set": {
                "status": "accepted",
                "reviewed_by": actor_id,
                "reviewed_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return sug


def reject_suggestion(
    db,
    *,
    tenant_id: str,
    lead_id: str,
    suggestion_id: str,
    actor_id: str,
) -> bool:
    if not ObjectId.is_valid(suggestion_id):
        return False
    res = db.ai_suggestions.update_one(
        {
            "_id": ObjectId(suggestion_id),
            "tenant_id": tenant_id,
            "lead_id": lead_id,
            "status": "pending",
        },
        {
            "$set": {
                "status": "rejected",
                "reviewed_by": actor_id,
                "reviewed_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )
    return res.modified_count > 0
