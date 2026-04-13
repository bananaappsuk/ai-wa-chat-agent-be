from datetime import datetime, timezone
from bson import ObjectId
from app.db.mongo import get_db
from app.models.common import utcnow


def _norm_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    p = phone.strip().replace(" ", "")
    if not p:
        return None
    if not p.startswith("+"):
        if p.startswith("0"):
            p = "+44" + p[1:]
        elif p.startswith("44"):
            p = "+" + p
        else:
            p = "+" + p
    return p


async def list_leads(user_id: str) -> list[dict]:
    cur = get_db().leads.find({"user_id": user_id}).sort("updated_at", -1)
    return [d async for d in cur]


async def get_lead(user_id: str, lead_id: str) -> dict | None:
    if not ObjectId.is_valid(lead_id):
        return None
    return await get_db().leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})


async def find_or_create_by_phone(user_id: str, phone: str, name: str | None = None, source: str = "whatsapp") -> dict:
    db = get_db()
    p = _norm_phone(phone)
    existing = await db.leads.find_one({"user_id": user_id, "phone": p})
    if existing:
        return existing
    now = utcnow()
    doc = {
        "user_id": user_id,
        "name": name or (p or "Unknown"),
        "phone": p,
        "score": "warm",
        "source": source,
        "tags": [],
        "blacklisted": False,
        "created_at": now,
        "updated_at": now,
    }
    res = await db.leads.insert_one(doc)
    doc["_id"] = res.inserted_id
    return doc


async def create_lead(user_id: str, payload: dict) -> dict:
    db = get_db()
    p = _norm_phone(payload.get("phone"))
    if p:
        existing = await db.leads.find_one({"user_id": user_id, "phone": p})
        if existing:
            return existing
    now = utcnow()
    doc = {
        "user_id": user_id,
        "name": payload["name"],
        "phone": p,
        "score": payload.get("score", "cold"),
        "source": payload.get("source"),
        "tags": payload.get("tags", []),
        "blacklisted": False,
        "created_at": now,
        "updated_at": now,
    }
    res = await db.leads.insert_one(doc)
    doc["_id"] = res.inserted_id
    return doc


async def update_lead(user_id: str, lead_id: str, payload: dict) -> dict | None:
    if not ObjectId.is_valid(lead_id):
        return None
    db = get_db()
    update = {k: v for k, v in payload.items() if v is not None}
    if "phone" in update:
        update["phone"] = _norm_phone(update["phone"])
    update["updated_at"] = utcnow()
    await db.leads.update_one({"_id": ObjectId(lead_id), "user_id": user_id}, {"$set": update})
    return await db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})


async def delete_lead(user_id: str, lead_id: str) -> bool:
    if not ObjectId.is_valid(lead_id):
        return False
    res = await get_db().leads.delete_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if res.deleted_count:
        await get_db().messages.delete_many({"lead_id": lead_id, "user_id": user_id})
    return bool(res.deleted_count)
