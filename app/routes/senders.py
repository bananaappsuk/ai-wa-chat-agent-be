from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.db.mongo import get_db
from app.middleware.auth import current_admin, current_user
from app.middleware.security import get_request_id
from app.models.common import serialize, utcnow
from app.security.audit import audit
from app.services import senders

router = APIRouter(prefix="/whatsapp-senders", tags=["whatsapp-senders"])


class SenderUpdate(BaseModel):
    label: Optional[str] = Field(default=None, max_length=60)
    enabled: Optional[bool] = None
    is_default: Optional[bool] = None


async def _listing(user_id: str) -> dict:
    db = get_db()
    items = [serialize(d) async for d in db.whatsapp_senders.find({"user_id": user_id}).sort("phone", 1)]
    return {"senders": items, "env_sender": senders.env_sender(), "expected_webhooks": senders.expected_webhooks()}


@router.get("")
async def list_senders(user: dict = Depends(current_user)) -> dict:
    return await _listing(str(user["_id"]))


@router.post("/sync")
async def sync_senders(user: dict = Depends(current_admin)) -> dict:
    """Pull the WhatsApp senders on the Twilio account (status, quality, webhook) into this account."""
    try:
        await senders.sync_senders(get_db(), str(user["_id"]))
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    audit("whatsapp_senders.sync", user_id=str(user["_id"]), request_id=get_request_id())
    return await _listing(str(user["_id"]))


async def _owned(sender_id: str, user_id: str) -> dict:
    if not ObjectId.is_valid(sender_id):
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db()[senders.COLLECTION].find_one({"_id": ObjectId(sender_id), "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return doc


@router.patch("/{sender_id}")
async def update_sender(sender_id: str, payload: SenderUpdate, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    db = get_db()
    doc = await _owned(sender_id, user_id)
    update: dict = {"updated_at": utcnow()}
    if payload.label is not None:
        update["label"] = payload.label.strip()
    if payload.enabled is not None:
        update["enabled"] = payload.enabled
        if not payload.enabled:
            update["is_default"] = False
    if payload.is_default:
        if payload.enabled is False or (payload.enabled is None and not doc.get("enabled")):
            raise HTTPException(status_code=400, detail="Switch the number on before making it the default.")
        await db.whatsapp_senders.update_many({"user_id": user_id}, {"$set": {"is_default": False}})
        update["is_default"] = True
    await db.whatsapp_senders.update_one({"_id": doc["_id"]}, {"$set": update})
    audit("whatsapp_senders.update", user_id=user_id, target_id=sender_id, request_id=get_request_id(),
          extra={k: v for k, v in payload.model_dump(exclude_none=True).items()})
    return await _listing(user_id)


@router.post("/{sender_id}/fix-webhook")
async def fix_webhook(sender_id: str, user: dict = Depends(current_admin)) -> dict:
    """Point the number's incoming messages and delivery receipts at this app."""
    user_id = str(user["_id"])
    doc = await _owned(sender_id, user_id)
    if not doc.get("sender_sid"):
        raise HTTPException(status_code=400, detail="Sync the numbers from Twilio first.")
    try:
        await run_in_threadpool(senders.fix_twilio_webhook, doc["sender_sid"])
        await senders.sync_senders(get_db(), user_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    audit("whatsapp_senders.fix_webhook", user_id=user_id, target_id=sender_id, request_id=get_request_id())
    return await _listing(user_id)
