import asyncio

from fastapi import APIRouter, Request, Response, HTTPException
from bson import ObjectId

from app.config import settings
from app.db.mongo import get_db
from app.models.common import utcnow
from app.services import twilio_service, lead_service
from app.services.ws_manager import ws_manager
from app.workers.queue import enqueue
from app.workers import tasks

router = APIRouter(tags=["webhook"])

STOP_WORDS = {"stop", "unsubscribe", "cancel", "end", "quit"}


def _serialize_for_ws(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
        elif isinstance(v, ObjectId):
            out[k] = str(v)
        else:
            try:
                out[k] = v.isoformat()  # type: ignore[attr-defined]
            except AttributeError:
                out[k] = v
    return out


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request) -> Response:
    form = await request.form()
    params = {k: v for k, v in form.items()}

    signature = request.headers.get("X-Twilio-Signature", "")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.url.netloc)
    full_url = f"{proto}://{host}{request.url.path}"

    if not twilio_service.validate_signature(full_url, params, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    db = get_db()
    twilio_sid = params.get("MessageSid") or params.get("SmsMessageSid")
    if twilio_sid:
        existing = await db.webhook_events.find_one({"twilio_sid": twilio_sid})
        if existing:
            return Response(content="<Response/>", media_type="application/xml")
        try:
            await db.webhook_events.insert_one({"twilio_sid": twilio_sid, "received_at": utcnow()})
        except Exception:
            return Response(content="<Response/>", media_type="application/xml")

    from_raw = params.get("From", "")
    to_raw = params.get("To", "")
    body = (params.get("Body") or "").strip()
    profile_name = params.get("ProfileName") or None
    if not from_raw:
        return Response(content="<Response/>", media_type="application/xml")

    from_phone = twilio_service.from_whatsapp(from_raw)

    user = None
    if to_raw:
        user = await db.users.find_one({"twilio_whatsapp_to": twilio_service.from_whatsapp(to_raw)})
    if not user:
        user = await db.users.find_one({"role": "admin"})
    if not user:
        return Response(content="<Response/>", media_type="application/xml")
    user_id = str(user["_id"])

    if await db.blacklist.find_one({"user_id": user_id, "phone": from_phone}):
        return Response(content="<Response/>", media_type="application/xml")

    is_new_lead = not await db.leads.find_one({"user_id": user_id, "phone": from_phone})
    lead = await lead_service.find_or_create_by_phone(user_id, from_phone, name=profile_name, source="whatsapp")
    lead_id = str(lead["_id"])

    inbound = {
        "user_id": user_id,
        "lead_id": lead_id,
        "direction": "inbound",
        "message": body,
        "status": "received",
        "twilio_sid": twilio_sid,
        "created_at": utcnow(),
    }
    res = await db.messages.insert_one(inbound)
    inbound["_id"] = res.inserted_id
    await ws_manager.push(user_id, "message:new", _serialize_for_ws(inbound))
    await db.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": {"updated_at": utcnow()}})

    if body.lower() in STOP_WORDS:
        await db.blacklist.update_one(
            {"user_id": user_id, "phone": from_phone},
            {"$set": {"created_at": utcnow()}},
            upsert=True,
        )
        await db.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": {"blacklisted": True}})
        confirm = (
            "You've been unsubscribed and will no longer receive messages from us. "
            "Reply START at any time to opt back in."
        )
        try:
            await asyncio.to_thread(twilio_service.send_whatsapp, from_phone, confirm)
        except Exception:
            pass
        return Response(content="<Response/>", media_type="application/xml")

    if body.lower() == "start":
        await db.blacklist.delete_one({"user_id": user_id, "phone": from_phone})
        await db.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": {"blacklisted": False}})

    if is_new_lead:
        agent = await db.agents.find_one({"user_id": user_id, "status": "active"}, sort=[("updated_at", -1)])
        if agent:
            welcome = agent.get("welcome_message")
            terms = agent.get("terms_text")
            if welcome:
                try:
                    await asyncio.to_thread(twilio_service.send_whatsapp, from_phone, welcome)
                    await db.messages.insert_one({
                        "user_id": user_id, "lead_id": lead_id, "direction": "outbound",
                        "message": welcome, "status": "sent", "created_at": utcnow(),
                    })
                except Exception:
                    pass
            if terms:
                try:
                    await asyncio.to_thread(twilio_service.send_whatsapp, from_phone, terms)
                    await db.messages.insert_one({
                        "user_id": user_id, "lead_id": lead_id, "direction": "outbound",
                        "message": terms, "status": "sent", "created_at": utcnow(),
                    })
                except Exception:
                    pass

    enqueue(tasks.generate_and_send_ai_reply, user_id, lead_id)
    return Response(content="<Response/>", media_type="application/xml")
