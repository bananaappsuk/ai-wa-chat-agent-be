from bson import ObjectId
from app.db.mongo import get_db
from app.models.common import utcnow


async def list_messages(user_id: str, lead_id: str, limit: int = 200) -> list[dict]:
    cur = (
        get_db()
        .messages.find({"user_id": user_id, "lead_id": lead_id})
        .sort("created_at", 1)
        .limit(limit)
    )
    return [d async for d in cur]


async def insert_message(
    user_id: str,
    lead_id: str,
    direction: str,
    message: str,
    status: str = "queued",
    twilio_sid: str | None = None,
    error: str | None = None,
) -> dict:
    doc = {
        "user_id": user_id,
        "lead_id": lead_id,
        "direction": direction,
        "message": message,
        "status": status,
        "twilio_sid": twilio_sid,
        "error": error,
        "created_at": utcnow(),
    }
    res = await get_db().messages.insert_one(doc)
    doc["_id"] = res.inserted_id
    await get_db().leads.update_one(
        {"_id": ObjectId(lead_id)}, {"$set": {"updated_at": utcnow()}}
    )
    return doc


async def mark_status(message_id: str, status: str, twilio_sid: str | None = None, error: str | None = None) -> None:
    if not ObjectId.is_valid(message_id):
        return
    update: dict = {"status": status}
    if twilio_sid:
        update["twilio_sid"] = twilio_sid
    if error:
        update["error"] = error
    await get_db().messages.update_one({"_id": ObjectId(message_id)}, {"$set": update})


async def history_for_ai(user_id: str, lead_id: str, limit: int = 20) -> list[dict]:
    cur = (
        get_db()
        .messages.find({"user_id": user_id, "lead_id": lead_id})
        .sort("created_at", -1)
        .limit(limit)
    )
    msgs = [d async for d in cur]
    return list(reversed(msgs))
