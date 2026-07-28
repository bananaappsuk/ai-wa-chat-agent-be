"""RQ jobs for AI summaries, extraction, classification (bulk/default queues)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from bson import ObjectId
from pymongo import MongoClient
from redis import Redis

from app.config import settings


_mongo: MongoClient | None = None


def _db():
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(settings.MONGO_URI)
    return _mongo[settings.MONGO_DB]


def _publish(user_id: str, event: str, data: dict) -> None:
    payload = json.dumps({"event": event, "data": data, "user_id": user_id}, default=str)
    Redis.from_url(settings.REDIS_URL).publish("ws:events", payload)


def _serialize(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
        elif isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def refresh_conversation_summary(user_id: str, lead_id: str) -> None:
    from app.services.ai_summary import refresh_summary_sync

    db = _db()
    doc = refresh_summary_sync(db, tenant_id=user_id, lead_id=lead_id, force=True)
    if doc:
        _publish(user_id, "conversation:summary", _serialize(doc))


def extract_lead_suggestions(user_id: str, lead_id: str) -> None:
    from app.services.ai_extraction import extract_suggestions_sync

    db = _db()
    created = extract_suggestions_sync(db, tenant_id=user_id, lead_id=lead_id)
    if created:
        _publish(
            user_id,
            "lead:ai_suggestions",
            {"lead_id": lead_id, "count": len(created), "items": [_serialize(x) for x in created]},
        )


def classify_latest_inbound(user_id: str, lead_id: str, message_text: str = "") -> None:
    from app.services.ai_classify import apply_classification_to_lead, should_auto_escalate
    from app.services.notifications import create_notification_sync

    db = _db()
    text = message_text
    if not text:
        msg = db.messages.find_one(
            {"user_id": user_id, "lead_id": lead_id, "direction": "inbound"},
            sort=[("created_at", -1)],
        )
        text = (msg or {}).get("message") or ""
    result = apply_classification_to_lead(db, tenant_id=user_id, lead_id=lead_id, text=text)
    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if lead:
        _publish(
            user_id,
            "lead:classified",
            {
                "lead_id": lead_id,
                "current_intent": result.get("current_intent"),
                "current_sentiment": result.get("current_sentiment"),
                "intent_confidence": result.get("intent_confidence"),
                "sentiment_confidence": result.get("sentiment_confidence"),
            },
        )
    if should_auto_escalate(result):
        create_notification_sync(
            db,
            user_id=user_id,
            type="needs_human",
            title="Urgent / negative conversation",
            message="A conversation was auto-escalated based on sentiment.",
            resource_type="lead",
            resource_id=lead_id,
            dedupe_key=f"escalate:{lead_id}:{result.get('current_sentiment')}",
        )
