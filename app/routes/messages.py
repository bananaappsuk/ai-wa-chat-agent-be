from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.message import MessageSend
from app.models.common import serialize
from app.services import message_service, lead_service
from app.services.ws_manager import ws_manager
from app.workers.queue import enqueue
from app.workers import tasks

router = APIRouter(tags=["messages"])


@router.get("/messages/{lead_id}")
async def list_messages(lead_id: str, user: dict = Depends(current_user)) -> list[dict]:
    lead = await lead_service.get_lead(str(user["_id"]), lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    docs = await message_service.list_messages(str(user["_id"]), lead_id)
    return [serialize(d) for d in docs]


@router.post("/send-message", status_code=202)
async def send_message(payload: MessageSend, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    lead = await lead_service.get_lead(user_id, payload.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if lead.get("blacklisted"):
        raise HTTPException(status_code=400, detail="Lead is unsubscribed")
    if not lead.get("phone"):
        raise HTTPException(status_code=400, detail="Lead has no phone number")

    doc = await message_service.insert_message(
        user_id=user_id,
        lead_id=payload.lead_id,
        direction="outbound",
        message=payload.message,
        status="queued",
    )
    serialized = serialize(doc)
    await ws_manager.push(user_id, "message:new", serialized)

    enqueue(
        tasks.send_outbound_message,
        str(doc["_id"]),
        user_id,
        payload.lead_id,
        payload.message,
        payload.media_url,
    )
    return serialized
