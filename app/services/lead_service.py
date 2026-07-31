from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_db
from app.models.common import utcnow, serialize
from app.services.phone_norm import normalize_e164


def _norm_phone(phone: str | None) -> str | None:
    """Backward-compatible alias for canonical E.164 normalisation."""
    return normalize_e164(phone)


def ai_suppressed(lead: dict | None) -> bool:
    """True when AI must not generate or send replies for this lead."""
    if not lead:
        return True
    if lead.get("ai_paused"):
        return True
    if lead.get("takeover_by"):
        return True
    return False


def _takeover_defaults(doc: dict) -> dict:
    from app.services.whatsapp_consent import apply_consent_defaults

    doc.setdefault("ai_paused", False)
    doc.setdefault("needs_human", False)
    doc.setdefault("takeover_by", None)
    doc.setdefault("takeover_at", None)
    doc.setdefault("last_inbound_at", None)
    doc.setdefault("whatsapp_window_expires_at", None)
    # Legacy leads may lack lead_score — keep working with null numeric.
    if "lead_score" not in doc:
        doc["lead_score"] = None
    if "score_updated_at" not in doc:
        doc["score_updated_at"] = None
    apply_consent_defaults(doc)
    return doc


async def list_leads(user_id: str) -> list[dict]:
    cur = get_db().leads.find({"user_id": user_id}).sort("updated_at", -1)
    return [_takeover_defaults(d) async for d in cur]


async def get_lead(user_id: str, lead_id: str) -> dict | None:
    if not ObjectId.is_valid(lead_id):
        return None
    doc = await get_db().leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    return _takeover_defaults(doc) if doc else None


async def find_or_create_by_phone(user_id: str, phone: str, name: str | None = None, source: str = "whatsapp") -> dict:
    from app.services.whatsapp_consent import CONSENT_DEFAULTS

    db = get_db()
    p = normalize_e164(phone)
    if not p:
        raise ValueError("Invalid phone number")
    existing = await db.leads.find_one({"user_id": user_id, "phone": p})
    if existing:
        return _takeover_defaults(existing)
    now = utcnow()
    doc = {
        "user_id": user_id,
        "name": name or p,
        "phone": p,
        "score": "cold",
        "lead_score": 0,
        "score_updated_at": now,
        "source": source,
        "tags": [],
        "blacklisted": False,
        "ai_paused": False,
        "needs_human": False,
        "takeover_by": None,
        "takeover_at": None,
        "last_inbound_at": None,
        "whatsapp_window_expires_at": None,
        **CONSENT_DEFAULTS,
        "created_at": now,
        "updated_at": now,
    }
    try:
        res = await db.leads.insert_one(doc)
        doc["_id"] = res.inserted_id
        return doc
    except DuplicateKeyError:
        existing = await db.leads.find_one({"user_id": user_id, "phone": p})
        if existing:
            return _takeover_defaults(existing)
        raise


async def create_lead(user_id: str, payload: dict) -> dict:
    from app.services.lead_scoring import recalculate_lead_score
    from app.services.whatsapp_consent import CONSENT_DEFAULTS

    db = get_db()
    p = normalize_e164(payload.get("phone"))
    if not p:
        raise ValueError("Invalid phone number")
    existing = await db.leads.find_one({"user_id": user_id, "phone": p})
    if existing:
        return _takeover_defaults(existing)
    now = utcnow()
    # Never auto opt-in on import/manual create
    doc = {
        "user_id": user_id,
        "name": payload["name"],
        "phone": p,
        "score": "cold",
        "lead_score": 0,
        "score_updated_at": now,
        "source": payload.get("source"),
        "tags": payload.get("tags", []),
        "blacklisted": False,
        "ai_paused": False,
        "needs_human": False,
        "takeover_by": None,
        "takeover_at": None,
        "last_inbound_at": None,
        "whatsapp_window_expires_at": None,
        **CONSENT_DEFAULTS,
        "created_at": now,
        "updated_at": now,
    }
    try:
        res = await db.leads.insert_one(doc)
    except DuplicateKeyError:
        existing = await db.leads.find_one({"user_id": user_id, "phone": p})
        if existing:
            return _takeover_defaults(existing)
        raise
    doc["_id"] = res.inserted_id
    scored = await recalculate_lead_score(user_id, str(res.inserted_id))
    return _takeover_defaults(scored or doc)


async def update_lead(user_id: str, lead_id: str, payload: dict) -> dict | None:
    from app.services.lead_scoring import recalculate_lead_score

    if not ObjectId.is_valid(lead_id):
        return None
    db = get_db()
    update = {k: v for k, v in payload.items() if v is not None}
    # Score is computed automatically — ignore client-provided score labels.
    update.pop("score", None)
    update.pop("lead_score", None)
    update.pop("score_updated_at", None)
    if "phone" in update:
        update["phone"] = _norm_phone(update["phone"])
    update["updated_at"] = utcnow()
    await db.leads.update_one({"_id": ObjectId(lead_id), "user_id": user_id}, {"$set": update})
    if "blacklisted" in update or "tags" in update or "phone" in update:
        await recalculate_lead_score(user_id, lead_id)
    return await get_lead(user_id, lead_id)


async def set_lead_control(user_id: str, lead_id: str, fields: dict) -> dict | None:
    """Set takeover/pause flags. Allows explicit nulls (e.g. clear takeover_by)."""
    if not ObjectId.is_valid(lead_id):
        return None
    db = get_db()
    update = dict(fields)
    update["updated_at"] = utcnow()
    res = await db.leads.update_one({"_id": ObjectId(lead_id), "user_id": user_id}, {"$set": update})
    if res.matched_count == 0:
        return None
    return await get_lead(user_id, lead_id)


async def delete_lead(user_id: str, lead_id: str) -> bool:
    if not ObjectId.is_valid(lead_id):
        return False
    res = await get_db().leads.delete_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if res.deleted_count:
        await get_db().messages.delete_many({"lead_id": lead_id, "user_id": user_id})
    return bool(res.deleted_count)


def lead_control_payload(doc: dict) -> dict:
    s = serialize(doc)
    return {
        "id": s["id"],
        "ai_paused": bool(s.get("ai_paused")),
        "needs_human": bool(s.get("needs_human")),
        "takeover_by": s.get("takeover_by"),
        "takeover_at": s.get("takeover_at"),
        "updated_at": s.get("updated_at"),
    }
