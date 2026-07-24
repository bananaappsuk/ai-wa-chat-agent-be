"""Sync RQ tasks. These run in the worker process and use sync Mongo + Redis."""
import json
from datetime import datetime, timezone
from typing import Optional
from bson import ObjectId
from pymongo import MongoClient
from redis import Redis

from app.config import settings
from app.services import twilio_service, openai_service

_mongo_client: MongoClient | None = None
_redis_client: Redis | None = None


def _db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(settings.MONGO_URI)
    return _mongo_client[settings.MONGO_DB]


def _redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.REDIS_URL,
            health_check_interval=60,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
    return _redis_client


def _publish(user_id: str, event: str, data: dict) -> None:
    payload = json.dumps({"event": event, "data": data, "user_id": user_id})
    _redis().publish("ws:events", payload)


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


def send_outbound_message(message_id: str, user_id: str, lead_id: str, body: str, media_url: Optional[str] = None) -> None:
    db = _db()
    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if not lead or not lead.get("phone"):
        db.messages.update_one(
            {"_id": ObjectId(message_id)}, {"$set": {"status": "failed", "error": "missing phone"}}
        )
        return
    try:
        result = twilio_service.send_whatsapp(lead["phone"], body, media_url)
        db.messages.update_one(
            {"_id": ObjectId(message_id)},
            {"$set": {"status": result.get("status") or "sent", "twilio_sid": result.get("sid")}},
        )
        msg = db.messages.find_one({"_id": ObjectId(message_id)})
        if msg:
            _publish(user_id, "message:updated", _serialize(msg))
    except Exception as exc:
        db.messages.update_one(
            {"_id": ObjectId(message_id)},
            {"$set": {"status": "failed", "error": str(exc)[:500]}},
        )
        msg = db.messages.find_one({"_id": ObjectId(message_id)})
        if msg:
            _publish(user_id, "message:updated", _serialize(msg))


def generate_and_send_ai_reply(user_id: str, lead_id: str) -> None:
    db = _db()
    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if not lead or lead.get("blacklisted"):
        return
    user = db.users.find_one({"_id": ObjectId(user_id)})
    company = (user or {}).get("company_name")
    agent = db.agents.find_one({"user_id": user_id, "status": "active"}, sort=[("updated_at", -1)])

    history_cur = db.messages.find({"user_id": user_id, "lead_id": lead_id}).sort("created_at", -1).limit(settings.OPENAI_MAX_HISTORY)
    history = list(reversed(list(history_cur)))

    try:
        reply = openai_service.generate_reply(agent, history, company)
    except Exception as exc:
        db.messages.insert_one({
            "user_id": user_id,
            "lead_id": lead_id,
            "direction": "outbound",
            "message": "(AI temporarily unavailable — a human will reply shortly.)",
            "status": "failed",
            "error": str(exc)[:500],
            "created_at": datetime.now(timezone.utc),
        })
        return

    if not reply:
        return

    msg_doc = {
        "user_id": user_id,
        "lead_id": lead_id,
        "direction": "outbound",
        "message": reply,
        "status": "queued",
        "twilio_sid": None,
        "error": None,
        "created_at": datetime.now(timezone.utc),
    }
    res = db.messages.insert_one(msg_doc)
    msg_doc["_id"] = res.inserted_id
    _publish(user_id, "message:new", _serialize(msg_doc))

    try:
        result = twilio_service.send_whatsapp(lead["phone"], reply)
        db.messages.update_one(
            {"_id": res.inserted_id},
            {"$set": {"status": result.get("status") or "sent", "twilio_sid": result.get("sid")}},
        )
    except Exception as exc:
        db.messages.update_one(
            {"_id": res.inserted_id},
            {"$set": {"status": "failed", "error": str(exc)[:500]}},
        )


def send_blast_messages(user_id: str, blast_id: str) -> None:
    db = _db()
    blast = db.blast_campaigns.find_one({"_id": ObjectId(blast_id), "user_id": user_id})
    if not blast:
        return
    db.blast_campaigns.update_one({"_id": ObjectId(blast_id)}, {"$set": {"status": "sending"}})

    sent = 0
    failed = 0
    cur = db.blast_recipients.find({"blast_id": blast_id, "status": "pending"})
    for r in cur:
        try:
            result = twilio_service.send_whatsapp(r["phone"], blast["message"])
            db.blast_recipients.update_one(
                {"_id": r["_id"]},
                {"$set": {"status": "sent", "twilio_sid": result.get("sid")}},
            )
            sent += 1
        except Exception as exc:
            db.blast_recipients.update_one(
                {"_id": r["_id"]}, {"$set": {"status": "failed", "error": str(exc)[:500]}}
            )
            failed += 1

    final_status = "completed" if failed == 0 else ("failed" if sent == 0 else "completed")
    db.blast_campaigns.update_one(
        {"_id": ObjectId(blast_id)},
        {"$set": {"sent_count": sent, "failed_count": failed, "status": final_status}},
    )
    _publish(user_id, "blast:updated", {"id": blast_id, "sent": sent, "failed": failed, "status": final_status})
