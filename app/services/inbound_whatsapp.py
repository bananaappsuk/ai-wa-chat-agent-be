"""Provider-neutral inbound WhatsApp CRM pipeline (Phase 2A).

Provider routes own webhook auth, payload parse, and HTTP response shape.
This module owns tenant routing, dedupe, lead/message persist, consent, window, WS.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_db
from app.models.common import serialize, utcnow
from app.services import lead_service
from app.services.lead_scoring import recalculate_lead_score
from app.services.phone_norm import is_self_sender, normalize_e164
from app.services.whatsapp_window import inbound_window_fields
from app.services.ws_manager import ws_manager

logger = logging.getLogger(__name__)

_MEDIA_PLACEHOLDERS = {
    "image": "[image]",
    "sticker": "[image]",
    "document": "[document]",
    "audio": "[audio]",
    "voice": "[audio]",
    "video": "[video]",
}


@dataclass(frozen=True)
class InboundMessage:
    provider: str
    provider_message_id: str
    customer_phone: str
    business_identifier: str
    body: str
    profile_name: str | None
    timestamp: str | None
    message_type: str
    skip_provider_outbound: bool = False
    skip_ai_jobs: bool = False
    business_display_phone: str | None = None
    media_meta: dict[str, Any] | None = None


@dataclass
class InboundResult:
    outcome: str
    user_id: str | None = None
    lead_id: str | None = None
    is_new_lead: bool = False
    enqueue_welcome: bool = False
    enqueue_classify: bool = False
    enqueue_ai: bool = False
    body: str = ""


def _placeholder_body(message_type: str, body: str, media_meta: dict[str, Any] | None) -> str:
    text = (body or "").strip()
    if text:
        return text
    if media_meta:
        filename = (media_meta.get("media_filename") or "").strip()
        if filename:
            return filename
        mt = (media_meta.get("message_type") or message_type or "").strip().lower()
        if mt in _MEDIA_PLACEHOLDERS:
            return _MEDIA_PLACEHOLDERS[mt]
        if mt and mt not in ("text", "unknown", ""):
            return f"[{mt}]"
    mt = (message_type or "").strip().lower()
    if mt in _MEDIA_PLACEHOLDERS:
        return _MEDIA_PLACEHOLDERS[mt]
    if mt and mt not in ("text", "unknown", ""):
        return f"[{mt}]"
    return text


async def _claim_webhook_event(db, inbound: InboundMessage) -> bool:
    """Return True if this delivery is new; False if it is a duplicate."""
    mid = (inbound.provider_message_id or "").strip()
    provider = (inbound.provider or "").strip().lower()
    if provider == "twilio":
        if not mid:
            return True
        existing = await db.webhook_events.find_one({"twilio_sid": mid})
        if existing:
            return False
        try:
            await db.webhook_events.insert_one(
                {
                    "twilio_sid": mid,
                    "provider": "twilio",
                    "provider_message_id": mid,
                    "received_at": utcnow(),
                }
            )
        except DuplicateKeyError:
            return False
        return True
    if provider == "meta":
        if not mid:
            return False
        existing = await db.webhook_events.find_one(
            {"provider": "meta", "provider_message_id": mid}
        )
        if existing:
            return False
        try:
            await db.webhook_events.insert_one(
                {
                    "provider": "meta",
                    "provider_message_id": mid,
                    "received_at": utcnow(),
                }
            )
        except DuplicateKeyError:
            return False
        return True
    return False


async def _resolve_tenant(db, inbound: InboundMessage) -> dict | None:
    ident = (inbound.business_identifier or "").strip()
    provider = (inbound.provider or "").strip().lower()
    if not ident:
        return None
    if provider == "twilio":
        user = await db.users.find_one({"twilio_whatsapp_to": ident})
        if not user:
            norm = lead_service._norm_phone(ident)
            if norm and norm != ident:
                user = await db.users.find_one({"twilio_whatsapp_to": norm})
        return user
    if provider == "meta":
        return await db.users.find_one({"meta_phone_number_id": ident})
    return None


def _is_self_sender(inbound: InboundMessage, customer_phone: str, user: dict) -> bool:
    if is_self_sender(customer_phone, user):
        return True
    display = normalize_e164(inbound.business_display_phone)
    from_norm = normalize_e164(customer_phone)
    if display and from_norm and display == from_norm:
        return True
    return False


async def _attribute_campaign_reply(db, user_id: str, from_phone: str) -> None:
    from app.config import settings

    try:
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
        if not recent:
            return
        await db.campaign_recipients.update_one(
            {"_id": recent["_id"], "replied_at": None},
            {"$set": {"status": "replied", "replied_at": utcnow(), "updated_at": utcnow()}},
        )
        if recent.get("campaign_id") and ObjectId.is_valid(str(recent["campaign_id"])):
            await db.campaigns.update_one(
                {"_id": ObjectId(str(recent["campaign_id"]))},
                {"$inc": {"replied_count": 1}, "$set": {"updated_at": utcnow()}},
            )
            camp = await db.campaigns.find_one({"_id": ObjectId(str(recent["campaign_id"]))})
            if camp:
                await ws_manager.push(user_id, "campaign:updated", serialize(camp))
    except Exception:
        logger.exception("Campaign reply attribution failed")


async def process_inbound_message(
    inbound: InboundMessage,
    *,
    db: Any | None = None,
) -> InboundResult:
    db = db if db is not None else get_db()
    provider = (inbound.provider or "").strip().lower()
    mid = (inbound.provider_message_id or "").strip()

    if provider == "meta" and not mid:
        logger.warning("Inbound WhatsApp missing provider_message_id provider=meta")
        return InboundResult(outcome="missing_id")

    claimed = await _claim_webhook_event(db, inbound)
    if not claimed:
        return InboundResult(outcome="duplicate")

    customer_phone = normalize_e164(inbound.customer_phone) or ""
    if not customer_phone:
        logger.warning(
            "Inbound WhatsApp invalid customer phone provider=%s id=%s",
            provider,
            mid or "(none)",
        )
        return InboundResult(outcome="invalid_phone")

    user = await _resolve_tenant(db, inbound)
    if not user:
        logger.warning(
            "Inbound WhatsApp with no tenant match provider=%s ident=%s From=%s id=%s",
            provider,
            inbound.business_identifier or "(missing)",
            customer_phone,
            mid or "(none)",
        )
        return InboundResult(outcome="no_tenant")

    user_id = str(user["_id"])
    from app.config import settings
    from app.observability.metrics import inc_consent_opt_out, inc_consent_opt_in, inc_self_sender_loop
    from app.services.consent_ops import apply_consent_change
    from app.services.whatsapp_consent import is_optout_keyword, is_optin_keyword

    if _is_self_sender(inbound, customer_phone, user):
        logger.warning(
            "Ignoring self-sender inbound webhook From=%s user_id=%s id=%s",
            customer_phone,
            user_id,
            mid or "(none)",
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
                resource_id=mid or None,
                metadata={"from": customer_phone, "provider": provider},
            )
        except Exception:
            logger.exception("record_activity failed for self_sender_ignored")
        return InboundResult(outcome="self_sender", user_id=user_id)

    body = (inbound.body or "").strip()
    optout = is_optout_keyword(body)
    optin = is_optin_keyword(body)

    if await db.blacklist.find_one({"user_id": user_id, "phone": customer_phone}):
        if not (optin and settings.WHATSAPP_ALLOW_KEYWORD_REOPTIN):
            return InboundResult(outcome="blacklisted", user_id=user_id)

    is_new_lead = not await db.leads.find_one({"user_id": user_id, "phone": customer_phone})
    lead = await lead_service.find_or_create_by_phone(
        user_id, customer_phone, name=inbound.profile_name, source="whatsapp"
    )
    lead_id = str(lead["_id"])

    media_meta = inbound.media_meta or {}
    display_body = _placeholder_body(inbound.message_type, body, media_meta)

    msg_doc: dict[str, Any] = {
        "user_id": user_id,
        "lead_id": lead_id,
        "direction": "inbound",
        "message": display_body,
        "status": "received",
        "provider": provider,
        "provider_message_id": mid or None,
        "created_at": utcnow(),
        "message_type": (media_meta.get("message_type") or inbound.message_type or "text"),
    }
    if provider == "twilio":
        msg_doc["twilio_sid"] = mid or None
    if media_meta:
        for key in ("media_items", "media_url", "media_content_type", "media_filename"):
            if key in media_meta:
                msg_doc[key] = media_meta[key]

    try:
        res = await db.messages.insert_one(msg_doc)
    except DuplicateKeyError:
        return InboundResult(outcome="duplicate", user_id=user_id, lead_id=lead_id)
    msg_doc["_id"] = res.inserted_id
    await ws_manager.push(user_id, "message:new", serialize(msg_doc))

    window_fields = inbound_window_fields()
    await db.leads.update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {**window_fields, "updated_at": utcnow()}},
    )
    lead = {**lead, **window_fields}
    await ws_manager.push(user_id, "lead:updated", serialize({**lead, "_id": ObjectId(lead_id)}))

    await _attribute_campaign_reply(db, user_id, customer_phone)

    if optout:
        await apply_consent_change(
            db,
            user_id=user_id,
            lead_id=lead_id,
            status="opted_out",
            source="keyword_optout",
            proof=body[:200],
            reason="keyword_optout",
            phone=customer_phone,
        )
        await recalculate_lead_score(user_id, lead_id)
        lead = await lead_service.get_lead(user_id, lead_id)
        if lead:
            await ws_manager.push(user_id, "lead:updated", serialize(lead))
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

        already = bool((lead or {}).get("whatsapp_optout_confirmation_sent_at"))
        if (
            not inbound.skip_provider_outbound
            and settings.WHATSAPP_OPTOUT_CONFIRMATION_ENABLED
            and not already
        ):
            confirm = (settings.WHATSAPP_OPTOUT_CONFIRMATION_TEXT or "").strip()
            if confirm:
                try:
                    from app.services import twilio_service

                    result = await asyncio.to_thread(twilio_service.send_whatsapp, customer_phone, confirm)
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
        return InboundResult(
            outcome="opt_out",
            user_id=user_id,
            lead_id=lead_id,
            body=display_body,
        )

    if optin and settings.WHATSAPP_ALLOW_KEYWORD_REOPTIN:
        await apply_consent_change(
            db,
            user_id=user_id,
            lead_id=lead_id,
            status="opted_in",
            source="keyword_optin",
            proof=body[:200],
            phone=customer_phone,
        )
        await recalculate_lead_score(user_id, lead_id)
        lead = await lead_service.get_lead(user_id, lead_id)
        if lead:
            await ws_manager.push(user_id, "lead:updated", serialize(lead))
        inc_consent_opt_in()
    elif optin and not settings.WHATSAPP_ALLOW_KEYWORD_REOPTIN:
        return InboundResult(outcome="optin_ignored", user_id=user_id, lead_id=lead_id)

    await recalculate_lead_score(user_id, lead_id)
    lead = await lead_service.get_lead(user_id, lead_id) or lead

    enqueue_welcome = bool(is_new_lead and not inbound.skip_provider_outbound)
    enqueue_classify = not inbound.skip_ai_jobs
    enqueue_ai = False
    if (
        not inbound.skip_ai_jobs
        and settings.AI_FEATURES_ENABLED
        and (settings.OPENAI_API_KEY or "").strip()
        and not lead_service.ai_suppressed(lead)
        and not lead.get("blacklisted")
        and (lead.get("whatsapp_consent_status") or "") != "opted_out"
    ):
        owner = await db.users.find_one({"_id": ObjectId(user_id)}, {"ai_settings": 1})
        tenant_ai = (owner or {}).get("ai_settings") or {}
        if tenant_ai.get("enabled") is not False:
            enqueue_ai = True

    return InboundResult(
        outcome="ok",
        user_id=user_id,
        lead_id=lead_id,
        is_new_lead=is_new_lead,
        enqueue_welcome=enqueue_welcome,
        enqueue_classify=enqueue_classify,
        enqueue_ai=enqueue_ai,
        body=display_body,
    )
