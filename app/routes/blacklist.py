from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from bson import ObjectId

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.common import utcnow, serialize
from app.services.twilio_service import to_whatsapp

router = APIRouter(prefix="/blacklist", tags=["blacklist"])


class PhoneBody(BaseModel):
    phone: str
    reason: str | None = None


def _norm(phone: str) -> str:
    return to_whatsapp(phone).replace("whatsapp:", "")


@router.get("")
async def list_blacklist(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().blacklist.find({"user_id": str(user["_id"])}).sort("created_at", -1)
    return [serialize(d) async for d in cur]


@router.post("", status_code=201)
async def add_blacklist(body: PhoneBody, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    phone = _norm(body.phone)
    db = get_db()
    await db.blacklist.update_one(
        {"user_id": user_id, "phone": phone},
        {"$set": {"reason": body.reason, "created_at": utcnow()}},
        upsert=True,
    )
    await db.leads.update_many(
        {"user_id": user_id, "phone": phone}, {"$set": {"blacklisted": True}}
    )
    return {"phone": phone, "ok": True}


@router.delete("")
async def remove_blacklist(body: PhoneBody, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    phone = _norm(body.phone)
    db = get_db()
    await db.blacklist.delete_one({"user_id": user_id, "phone": phone})
    await db.leads.update_many(
        {"user_id": user_id, "phone": phone}, {"$set": {"blacklisted": False}}
    )
    return {"phone": phone, "ok": True}
