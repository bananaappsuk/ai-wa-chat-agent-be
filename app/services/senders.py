"""WhatsApp sender numbers kept in the app (synced from Twilio) and chosen per send, so changing
the sender never needs an env change or a restart. TWILIO_WHATSAPP_FROM stays the last fallback."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.services.phone_norm import normalize_e164

COLLECTION = "whatsapp_senders"


def expected_webhooks() -> dict[str, str]:
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    return {
        "callback_url": f"{base}/api/webhook/whatsapp",
        "callback_method": "POST",
        "status_callback_url": f"{base}/api/webhook/twilio/status",
        "status_callback_method": "POST",
    }


def env_sender() -> Optional[str]:
    return normalize_e164(settings.TWILIO_WHATSAPP_FROM)


def _webhook_ok(webhook: dict) -> bool:
    want = expected_webhooks()
    return (webhook or {}).get("callback_url") == want["callback_url"] and (webhook or {}).get("status_callback_url") == want["status_callback_url"]


def fetch_twilio_senders() -> list[dict[str, Any]]:
    import httpx

    r = httpx.get(
        "https://messaging.twilio.com/v2/Channels/Senders",
        params={"Channel": "whatsapp", "PageSize": 50},
        auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio senders list failed ({r.status_code})")
    out = []
    for s in r.json().get("senders") or []:
        phone = normalize_e164(s.get("sender_id"))
        if not phone:
            continue
        props, hook = s.get("properties") or {}, s.get("webhook") or {}
        out.append({
            "phone": phone,
            "sender_sid": s.get("sid"),
            "display_name": (s.get("profile") or {}).get("name") or None,
            "twilio_status": s.get("status"),
            "quality": props.get("quality_rating"),
            "messaging_limit": props.get("messaging_limit"),
            "callback_url": hook.get("callback_url"),
            "status_callback_url": hook.get("status_callback_url"),
            "webhook_ok": _webhook_ok(hook),
        })
    return out


def fix_twilio_webhook(sender_sid: str) -> None:
    import httpx

    r = httpx.post(
        f"https://messaging.twilio.com/v2/Channels/Senders/{sender_sid}",
        json={"webhook": expected_webhooks()},
        auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
        timeout=httpx.Timeout(30.0, connect=10.0),
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio could not update the sender's webhook ({r.status_code}): {r.text[:200]}")


async def sync_senders(db, user_id: str) -> list[dict]:
    """Pull the Twilio account's WhatsApp senders into this account. New ONLINE numbers start
    enabled; the first default is the env sender (or the first ONLINE number)."""
    from fastapi.concurrency import run_in_threadpool

    found = await run_in_threadpool(fetch_twilio_senders)
    now = datetime.now(timezone.utc)
    seen = set()
    for s in found:
        seen.add(s["phone"])
        existing = await db.whatsapp_senders.find_one({"phone": s["phone"]})
        if existing and existing.get("user_id") != user_id:
            continue
        fields = {**s, "synced_at": now}
        if existing:
            await db.whatsapp_senders.update_one({"_id": existing["_id"]}, {"$set": fields})
        else:
            await db.whatsapp_senders.insert_one({**fields, "user_id": user_id, "label": s["display_name"] or "",
                                             "enabled": s["twilio_status"] == "ONLINE", "is_default": False, "created_at": now})
    await db.whatsapp_senders.update_many({"user_id": user_id, "phone": {"$nin": list(seen)}},
                                     {"$set": {"twilio_status": "NOT_IN_TWILIO", "enabled": False, "synced_at": now}})
    if not await db.whatsapp_senders.find_one({"user_id": user_id, "is_default": True, "enabled": True}):
        env = env_sender()
        pick = await db.whatsapp_senders.find_one({"user_id": user_id, "phone": env, "enabled": True}) if env else None
        pick = pick or await db.whatsapp_senders.find_one({"user_id": user_id, "enabled": True, "twilio_status": "ONLINE"})
        if pick:
            await db.whatsapp_senders.update_many({"user_id": user_id}, {"$set": {"is_default": False}})
            await db.whatsapp_senders.update_one({"_id": pick["_id"]}, {"$set": {"is_default": True}})
    return [d async for d in db.whatsapp_senders.find({"user_id": user_id}).sort("phone", 1)]


async def resolve_sender(db, user_id: str, requested: Optional[str]) -> Optional[str]:
    """The number a campaign or blast sends from: the one requested (must be this account's and
    switched on), else the account's default, else None (= env fallback)."""
    if requested:
        phone = normalize_e164(requested)
        doc = await db.whatsapp_senders.find_one({"user_id": user_id, "phone": phone}) if phone else None
        if not doc:
            raise ValueError("That WhatsApp number isn't one of this account's senders.")
        if not doc.get("enabled"):
            raise ValueError(f"{phone} is switched off in Settings → WhatsApp numbers.")
        return phone
    doc = await db.whatsapp_senders.find_one({"user_id": user_id, "is_default": True, "enabled": True})
    return doc["phone"] if doc else None


async def tenant_user_for_number(db, number: Optional[str]) -> Optional[dict]:
    from bson import ObjectId

    phone = normalize_e164(number)
    doc = await db.whatsapp_senders.find_one({"phone": phone}, {"user_id": 1}) if phone else None
    if not doc or not ObjectId.is_valid(str(doc.get("user_id"))):
        return None
    return await db.users.find_one({"_id": ObjectId(doc["user_id"])})


async def is_own_sender(db, phone: Optional[str]) -> bool:
    p = normalize_e164(phone)
    return bool(p and await db.whatsapp_senders.find_one({"phone": p}, {"_id": 1}))


def default_sender_sync(db, user_id: str) -> Optional[str]:
    doc = db.whatsapp_senders.find_one({"user_id": user_id, "is_default": True, "enabled": True}, {"phone": 1})
    return doc["phone"] if doc else None


def reply_from_sync(db, user_id: str, lead: Optional[dict]) -> Optional[str]:
    """Replies go out from the number the contact last wrote to (if it's still switched on),
    so the conversation stays on one number; otherwise the default sender."""
    last = normalize_e164((lead or {}).get("last_business_number"))
    if last and db.whatsapp_senders.find_one({"user_id": user_id, "phone": last, "enabled": True}, {"_id": 1}):
        return last
    return default_sender_sync(db, user_id)


def all_sender_numbers_sync(db) -> set[str]:
    return {d["phone"] for d in db.whatsapp_senders.find({}, {"phone": 1}) if d.get("phone")}


_QUALITY_RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}


def refresh_senders_sync() -> int:
    """Every few minutes: refresh status, quality and webhook of every synced number from Twilio
    and tell the owner when a number goes offline, its quality drops or recovers, or its webhook
    stops pointing at the app."""
    from app.services.notifications import create_notification_sync
    from app.workers.tasks import _db

    db = _db()
    if not db.whatsapp_senders.find_one({}, {"_id": 1}):
        return 0
    live = {s["phone"]: s for s in fetch_twilio_senders()}
    now = datetime.now(timezone.utc)
    notified = 0
    for doc in db.whatsapp_senders.find({}):
        s = live.get(doc["phone"])
        if not s:
            if doc.get("twilio_status") != "NOT_IN_TWILIO":
                db.whatsapp_senders.update_one({"_id": doc["_id"]}, {"$set": {"twilio_status": "NOT_IN_TWILIO", "enabled": False, "is_default": False, "synced_at": now}})
            continue
        db.whatsapp_senders.update_one({"_id": doc["_id"]}, {"$set": {**s, "synced_at": now}})
        notes = []
        if doc.get("twilio_status") == "ONLINE" and s["twilio_status"] != "ONLINE":
            notes.append(("WhatsApp number offline", f"{doc['phone']} is now {s['twilio_status']} on Twilio — sends from it will fail."
                          + (" It's your default sender: pick another in Settings → WhatsApp numbers." if doc.get("is_default") else "")))
        elif doc.get("twilio_status") not in (None, "ONLINE") and s["twilio_status"] == "ONLINE":
            notes.append(("WhatsApp number back online", f"{doc['phone']} is ONLINE again."))
        old_q, new_q = _QUALITY_RANK.get(doc.get("quality") or ""), _QUALITY_RANK.get(s.get("quality") or "")
        if old_q and new_q and new_q < old_q:
            notes.append((f"Quality dropped to {s['quality']}", f"{doc['phone']} went from {doc['quality']} to {s['quality']}. People are blocking or "
                          "reporting messages — slow down or pause sends from this number, or WhatsApp may restrict it."))
        elif old_q and new_q and new_q > old_q:
            notes.append((f"Quality back to {s['quality']}", f"{doc['phone']} improved from {doc['quality']} to {s['quality']}."))
        if doc.get("webhook_ok", True) and not s["webhook_ok"]:
            notes.append(("Replies to a number aren't reaching the app", f"{doc['phone']}'s webhook no longer points at the app — press Fix webhook in Settings → WhatsApp numbers."))
        for title, message in notes:
            notified += 1
            create_notification_sync(db, user_id=doc["user_id"], type="system", title=title, message=message,
                                     resource_type="whatsapp_sender", resource_id=str(doc["_id"]),
                                     dedupe_key=f"sender:{doc['phone']}:{title}:{now:%Y%m%d%H}")
    return notified
