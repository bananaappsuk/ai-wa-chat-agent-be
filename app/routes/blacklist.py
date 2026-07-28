from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.middleware.security import get_request_id
from app.models.common import utcnow, serialize
from app.security.audit import audit
from app.security.validation import validate_e164_phone
from app.services.consent_ops import apply_consent_change
from app.services.twilio_service import to_whatsapp
from app.services.ws_manager import ws_manager

router = APIRouter(prefix="/blacklist", tags=["blacklist"])


class PhoneBody(BaseModel):
    phone: str = Field(min_length=5, max_length=32)
    reason: str | None = Field(default=None, max_length=200)


def _norm(phone: str) -> str:
    return to_whatsapp(phone).replace("whatsapp:", "")


@router.get("")
async def list_blacklist(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().blacklist.find({"user_id": str(user["_id"])}).sort("created_at", -1)
    return [serialize(d) async for d in cur]


@router.post("", status_code=201)
async def add_blacklist(body: PhoneBody, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    phone = validate_e164_phone(_norm(body.phone))
    db = get_db()
    await db.blacklist.update_one(
        {"user_id": user_id, "phone": phone},
        {"$set": {"reason": body.reason, "created_at": utcnow(), "source": "blacklist"}},
        upsert=True,
    )
    from app.services.lead_scoring import recalculate_lead_score

    async for lead in db.leads.find({"user_id": user_id, "phone": phone}):
        await apply_consent_change(
            db,
            user_id=user_id,
            lead_id=str(lead["_id"]),
            status="opted_out",
            source="blacklist",
            reason=body.reason or "blacklist",
            changed_by=user_id,
            phone=phone,
            request_id=get_request_id(),
        )
        await recalculate_lead_score(user_id, str(lead["_id"]))
        updated = await db.leads.find_one({"_id": lead["_id"]})
        if updated:
            await ws_manager.push(user_id, "lead:updated", serialize(updated))
    # No matching lead — still store blacklist entry (already upserted)
    if not await db.leads.find_one({"user_id": user_id, "phone": phone}):
        await db.leads.update_many(
            {"user_id": user_id, "phone": phone}, {"$set": {"blacklisted": True}}
        )
    audit("blacklist.add", user_id=user_id, request_id=get_request_id())
    return {"phone": phone, "ok": True}


@router.delete("")
async def remove_blacklist(body: PhoneBody, user: dict = Depends(current_user)) -> dict:
    """Remove blacklist entry. Does NOT automatically mark opted_in."""
    user_id = str(user["_id"])
    phone = validate_e164_phone(_norm(body.phone))
    db = get_db()
    await db.blacklist.delete_one({"user_id": user_id, "phone": phone})
    await db.leads.update_many(
        {"user_id": user_id, "phone": phone},
        {
            "$set": {
                "blacklisted": False,
                "updated_at": utcnow(),
                # Keep consent status — clearing blacklist ≠ opt-in
            }
        },
    )
    from app.services.lead_scoring import recalculate_lead_score

    async for lead in db.leads.find({"user_id": user_id, "phone": phone}, {"_id": 1}):
        await recalculate_lead_score(user_id, str(lead["_id"]))
        updated = await db.leads.find_one({"_id": lead["_id"]})
        if updated:
            await ws_manager.push(user_id, "lead:updated", serialize(updated))
    audit("blacklist.remove", user_id=user_id, request_id=get_request_id())
    return {"phone": phone, "ok": True, "note": "Consent status unchanged — explicit opt-in still required for marketing"}
