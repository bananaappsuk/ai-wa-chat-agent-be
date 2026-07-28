import asyncio
import logging

from fastapi import APIRouter, Request, Response, HTTPException
from bson import ObjectId

from app.db.mongo import get_db
from app.models.common import utcnow
from app.services import twilio_service, lead_service
from app.services.lead_scoring import recalculate_lead_score
from app.services.status_callback import apply_twilio_status_callback
from app.services.whatsapp_window import inbound_window_fields
from app.services.media import parse_inbound_media, media_fields_from_items
from app.services.ws_manager import ws_manager
from app.workers.queue import enqueue
from app.workers import tasks

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)


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


def _request_public_url(request: Request) -> str:
    """
    Reconstruct the externally visible URL for Twilio signature validation.

    Prefer PUBLIC_BASE_URL (canonical) when set. Otherwise only honour a limited
    number of reverse-proxy hops via TRUSTED_PROXY_COUNT — never blindly trust
    arbitrary X-Forwarded-* chains.
    """
    from app.config import settings

    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if base:
        return f"{base}{request.url.path}"

    proto = request.url.scheme
    host = request.url.netloc
    if settings.TRUSTED_PROXY_COUNT > 0:
        xf_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
        xf_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
        if xf_proto in ("http", "https"):
            proto = xf_proto
        if xf_host and "/" not in xf_host and " " not in xf_host:
            host = xf_host
    return f"{proto}://{host}{request.url.path}"


@router.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request) -> Response:
    from app.security.rate_limit import rate_limit_webhook

    rate_limit_webhook(request, status_callback=False)
    form = await request.form()
    params = {k: v for k, v in form.items()}

    signature = request.headers.get("X-Twilio-Signature", "")
    full_url = _request_public_url(request)

    if not twilio_service.validate_signature(full_url, params, signature):
        from app.observability.metrics import inc_webhook_sig_fail

        inc_webhook_sig_fail()
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    from app.observability.metrics import inc_webhook_inbound

    inc_webhook_inbound()
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

    to_phone = twilio_service.from_whatsapp(to_raw) if to_raw else ""
    user = None
    if to_phone:
        user = await db.users.find_one({"twilio_whatsapp_to": to_phone})
        if not user:
            # Match numbers saved via Settings normalization (e.g. leading zeros).
            norm = lead_service._norm_phone(to_phone)
            if norm and norm != to_phone:
                user = await db.users.find_one({"twilio_whatsapp_to": norm})
    if not user:
        logger.warning(
            "Inbound WhatsApp with no tenant match To=%s From=%s MessageSid=%s",
            to_phone or to_raw or "(missing)",
            from_phone,
            twilio_sid or "(none)",
        )
        return Response(content="<Response/>", media_type="application/xml")
    user_id = str(user["_id"])

    from app.config import settings
    from app.services.phone_norm import is_self_sender
    from app.services.whatsapp_consent import is_optout_keyword, is_optin_keyword
    from app.services.consent_ops import apply_consent_change
    from app.observability.metrics import inc_consent_opt_out, inc_consent_opt_in, inc_self_sender_loop

    # C7: never create a lead / trigger AI when the inbound "From" is our own
    # sending number — this indicates a loop (e.g. sandbox misconfiguration or
    # a forwarded webhook) rather than a real customer message.
    if is_self_sender(from_phone, user):
        logger.warning(
            "Ignoring self-sender inbound webhook From=%s user_id=%s MessageSid=%s",
            from_phone,
            user_id,
            twilio_sid or "(none)",
        )
        inc_self_sender_loop()
        try:
            from app.services.activity import record_activity

            await record_activity(
                db,
                tenant_id=user_id,
                event_type="self_sender_ignored",
                summary="Ignored inbound webhook matching our own WhatsApp sender number",
                resource_type="webhook",
                resource_id=twilio_sid,
                metadata={"from": from_phone},
            )
        except Exception:
            logger.exception("record_activity failed for self_sender_ignored")
        return Response(content="<Response/>", media_type="application/xml")

    optout = is_optout_keyword(body)
    optin = is_optin_keyword(body)

    # Blacklisted numbers: allow only keyword re-opt-in processing; otherwise drop quietly.
    if await db.blacklist.find_one({"user_id": user_id, "phone": from_phone}):
        if not (optin and settings.WHATSAPP_ALLOW_KEYWORD_REOPTIN):
            return Response(content="<Response/>", media_type="application/xml")

    is_new_lead = not await db.leads.find_one({"user_id": user_id, "phone": from_phone})
    lead = await lead_service.find_or_create_by_phone(user_id, from_phone, name=profile_name, source="whatsapp")
    lead_id = str(lead["_id"])

    media_items = parse_inbound_media(params)
    media_meta = media_fields_from_items(media_items, body)
    # Media-only inbound: Body may be empty — still store a display label.
    display_body = body
    if not display_body and media_items:
        display_body = media_meta.get("media_filename") or f"[{media_meta['message_type']}]"

    inbound = {
        "user_id": user_id,
        "lead_id": lead_id,
        "direction": "inbound",
        "message": display_body,
        "status": "received",
        "twilio_sid": twilio_sid,
        "created_at": utcnow(),
        **media_meta,
    }
    res = await db.messages.insert_one(inbound)
    inbound["_id"] = res.inserted_id
    await ws_manager.push(user_id, "message:new", _serialize_for_ws(inbound))

    window_fields = inbound_window_fields()
    await db.leads.update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {**window_fields, "updated_at": utcnow()}},
    )
    lead = {**lead, **window_fields}
    await ws_manager.push(user_id, "lead:updated", _serialize_for_ws({**lead, "_id": ObjectId(lead_id)}))

    # Campaign reply attribution (most recent sent recipient within window)
    try:
        from datetime import timedelta

        since = utcnow() - timedelta(hours=max(1, int(settings.CAMPAIGN_REPLY_WINDOW_HOURS)))
        recent = await db.campaign_recipients.find_one(
            {
                "user_id": user_id,
                "phone": from_phone,
                "status": {"$in": ["sent", "delivered", "read"]},
                "updated_at": {"$gte": since},
                "replied_at": None,
            },
            sort=[("updated_at", -1)],
        )
        if recent:
            await db.campaign_recipients.update_one(
                {"_id": recent["_id"], "replied_at": None},
                {"$set": {"status": "replied", "replied_at": utcnow(), "updated_at": utcnow()}},
            )
            # Increment replied_count once
            if recent.get("campaign_id") and ObjectId.is_valid(str(recent["campaign_id"])):
                await db.campaigns.update_one(
                    {"_id": ObjectId(str(recent["campaign_id"]))},
                    {"$inc": {"replied_count": 1}, "$set": {"updated_at": utcnow()}},
                )
                camp = await db.campaigns.find_one({"_id": ObjectId(str(recent["campaign_id"]))})
                if camp:
                    await ws_manager.push(user_id, "campaign:updated", _serialize_for_ws(camp))
    except Exception:
        logger.exception("Campaign reply attribution failed")

    if optout:
        await apply_consent_change(
            db,
            user_id=user_id,
            lead_id=lead_id,
            status="opted_out",
            source="keyword_optout",
            proof=body[:200],
            reason="keyword_optout",
            phone=from_phone,
        )
        await recalculate_lead_score(user_id, lead_id)
        lead = await lead_service.get_lead(user_id, lead_id)
        if lead:
            await ws_manager.push(user_id, "lead:updated", _serialize_for_ws(lead))
        inc_consent_opt_out()
        try:
            from app.services.notifications import create_notification
            from app.services.activity import record_activity

            await record_activity(
                db,
                tenant_id=user_id,
                event_type="consent.opt_out",
                summary="STOP/opt-out keyword received",
                actor_id=None,
                resource_type="lead",
                resource_id=lead_id,
            )
            await create_notification(
                db,
                user_id=user_id,
                type="consent_opt_out",
                title="WhatsApp opt-out",
                message="A contact replied STOP and was opted out.",
                resource_type="lead",
                resource_id=lead_id,
                dedupe_key=f"kw_optout:{lead_id}",
            )
        except Exception:
            logger.exception("opt-out notification failed")

        # One confirmation message max
        already = bool((lead or {}).get("whatsapp_optout_confirmation_sent_at"))
        if settings.WHATSAPP_OPTOUT_CONFIRMATION_ENABLED and not already:
            confirm = (settings.WHATSAPP_OPTOUT_CONFIRMATION_TEXT or "").strip()
            if confirm:
                try:
                    result = await asyncio.to_thread(twilio_service.send_whatsapp, from_phone, confirm)
                    await db.messages.insert_one(
                        {
                            "user_id": user_id,
                            "lead_id": lead_id,
                            "direction": "outbound",
                            "message": confirm,
                            "status": result.get("status") or "sent",
                            "twilio_sid": result.get("sid"),
                            "message_purpose": "opt_out_confirmation",
                            "created_at": utcnow(),
                        }
                    )
                    await db.leads.update_one(
                        {"_id": ObjectId(lead_id)},
                        {"$set": {"whatsapp_optout_confirmation_sent_at": utcnow()}},
                    )
                except Exception:
                    logger.exception("Opt-out confirmation send failed")
        return Response(content="<Response/>", media_type="application/xml")

    if optin and settings.WHATSAPP_ALLOW_KEYWORD_REOPTIN:
        await apply_consent_change(
            db,
            user_id=user_id,
            lead_id=lead_id,
            status="opted_in",
            source="keyword_optin",
            proof=body[:200],
            phone=from_phone,
        )
        await recalculate_lead_score(user_id, lead_id)
        lead = await lead_service.get_lead(user_id, lead_id)
        if lead:
            await ws_manager.push(user_id, "lead:updated", _serialize_for_ws(lead))
        inc_consent_opt_in()
    elif optin and not settings.WHATSAPP_ALLOW_KEYWORD_REOPTIN:
        # Keyword seen but policy forbids automatic re-opt-in
        return Response(content="<Response/>", media_type="application/xml")

    await recalculate_lead_score(user_id, lead_id)

    if is_new_lead:
        # B11: hand off to the worker instead of blocking the webhook request on
        # Twilio calls — the task re-checks eligibility and is idempotent.
        try:
            enqueue(tasks.send_welcome_and_terms, user_id, lead_id, queue="high")
        except Exception:
            logger.exception("Failed to enqueue welcome/terms send")

    # Always save + WS-publish inbound above; only enqueue AI when not paused / taken over / opted out.
    lead = await lead_service.get_lead(user_id, lead_id) or lead
    # Classification (rules-first) — does not require OpenAI
    try:
        from app.workers.queue import enqueue as _enq
        from app.workers import ai_tasks

        _enq(
            ai_tasks.classify_latest_inbound,
            user_id,
            lead_id,
            body or "",
            queue="default",
        )
    except Exception:
        logger.exception("classify enqueue failed")

    if (
        settings.AI_FEATURES_ENABLED
        and (settings.OPENAI_API_KEY or "").strip()
        and not lead_service.ai_suppressed(lead)
        and not lead.get("blacklisted")
        and (lead.get("whatsapp_consent_status") or "") != "opted_out"
    ):
        # Respect tenant AI disable
        owner = await db.users.find_one({"_id": ObjectId(user_id)}, {"ai_settings": 1})
        tenant_ai = (owner or {}).get("ai_settings") or {}
        if tenant_ai.get("enabled") is False:
            pass
        else:
            enqueue(tasks.generate_and_send_ai_reply, user_id, lead_id, queue="default")
    return Response(content="<Response/>", media_type="application/xml")


@router.post("/webhook/twilio/status")
async def twilio_status_callback(request: Request) -> Response:
    """Receive Twilio WhatsApp delivery status updates for outbound messages."""
    from app.security.rate_limit import rate_limit_webhook

    # Generous limit — Twilio can burst status callbacks; do not drop legitimate traffic.
    rate_limit_webhook(request, status_callback=True)
    form = await request.form()
    params = {k: v for k, v in form.items()}

    signature = request.headers.get("X-Twilio-Signature", "")
    full_url = _request_public_url(request)
    if not twilio_service.validate_signature(full_url, params, signature):
        from app.observability.metrics import inc_webhook_sig_fail

        inc_webhook_sig_fail()
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    twilio_sid = (params.get("MessageSid") or params.get("SmsSid") or "").strip()
    status_raw = (params.get("MessageStatus") or params.get("SmsStatus") or "").strip()
    error_code = params.get("ErrorCode")
    error_message = params.get("ErrorMessage")
    # To / From / AccountSid accepted for audit but never used as tenant identity.
    _ = params.get("To"), params.get("From"), params.get("AccountSid")

    if not twilio_sid or not status_raw:
        logger.warning("Twilio status callback missing MessageSid or MessageStatus")
        return Response(content="<Response/>", media_type="application/xml")

    try:
        await apply_twilio_status_callback(
            twilio_sid=twilio_sid,
            status_raw=status_raw,
            error_code=str(error_code) if error_code is not None else None,
            error_message=str(error_message) if error_message is not None else None,
        )
    except Exception:
        logger.exception(
            "Error applying Twilio status callback sid_suffix=...%s",
            twilio_sid[-6:] if len(twilio_sid) > 6 else "?",
        )
        raise HTTPException(status_code=500, detail="Temporary error processing status")
    return Response(content="<Response/>", media_type="application/xml")
