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
    template_id: str | None = None,
    content_sid: str | None = None,
    content_variables: dict | None = None,
    message_type: str | None = "text",
    media_items: list | None = None,
    media_url: str | None = None,
    media_content_type: str | None = None,
    media_filename: str | None = None,
    provider: str | None = None,
    sender_type: str | None = None,
    provider_message_id: str | None = None,
) -> dict:
    doc = {
        "user_id": user_id,
        "lead_id": lead_id,
        "direction": direction,
        "message": message,
        "status": status,
        "message_type": message_type or "text",
        "twilio_sid": twilio_sid,
        "error": error,
        "created_at": utcnow(),
    }
    if provider:
        doc["provider"] = provider
    if sender_type:
        doc["sender_type"] = sender_type
    if provider_message_id is not None:
        doc["provider_message_id"] = provider_message_id
    if template_id:
        doc["template_id"] = template_id
    if content_sid:
        doc["content_sid"] = content_sid
    if content_variables is not None:
        doc["content_variables"] = content_variables
    if media_items:
        doc["media_items"] = media_items
    if media_url:
        doc["media_url"] = media_url
    if media_content_type:
        doc["media_content_type"] = media_content_type
    if media_filename:
        doc["media_filename"] = media_filename
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
