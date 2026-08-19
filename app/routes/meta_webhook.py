"""Meta WhatsApp Cloud API webhooks (verify + Phase 2A inbound CRM)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.db.mongo import get_db
from app.services import meta_whatsapp_service
from app.services.inbound_whatsapp import InboundMessage, process_inbound_message
from app.services.status_callback import apply_meta_status_update

router = APIRouter(tags=["webhook-meta"])
logger = logging.getLogger(__name__)


@router.get("/webhook/meta/whatsapp")
async def meta_whatsapp_webhook_verify(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
) -> Response:
    """Meta webhook subscription handshake."""
    if meta_whatsapp_service.verify_webhook_subscribe(mode=hub_mode, token=hub_verify_token):
        logger.info("meta_webhook verify ok mode=%s", hub_mode)
        return Response(content=hub_challenge or "", media_type="text/plain", status_code=200)
    logger.warning("meta_webhook verify failed mode=%s", hub_mode)
    raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/webhook/meta/whatsapp")
async def meta_whatsapp_webhook(request: Request) -> dict:
    """
    Accept Meta inbound + status webhooks.

    Phase 2A/2B: persist inbound text to CRM/Live Chat, then enqueue AI replies.
    Phase 2D: persist outbound delivery/read/failed statuses onto matching Meta rows.
    Welcome/STOP confirmation still skip provider outbound.
    """
    from app.security.rate_limit import rate_limit_webhook

    rate_limit_webhook(request, status_callback=False)

    raw = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")
    if not meta_whatsapp_service.verify_webhook_signature(raw_body=raw, signature_header=signature):
        logger.warning("meta_webhook invalid signature")
        raise HTTPException(status_code=403, detail="Invalid Meta signature")

    try:
        import json

        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except Exception:
        logger.warning("meta_webhook invalid json")
        raise HTTPException(status_code=400, detail="Invalid JSON") from None

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    messages = meta_whatsapp_service.parse_inbound_messages(payload)
    statuses = meta_whatsapp_service.parse_status_updates(payload)
    db = get_db()

    for msg in messages:
        text_len = len(msg.text) if msg.text is not None else 0
        logger.info(
            "meta_inbound provider=%s message_id=%s phone_number_id=%s from=%s "
            "message_type=%s text_len=%s timestamp=%s",
            msg.provider,
            msg.provider_message_id,
            msg.phone_number_id,
            msg.from_number,
            msg.message_type,
            text_len,
            msg.timestamp,
        )
        try:
            result = await process_inbound_message(
                InboundMessage(
                    provider="meta",
                    provider_message_id=msg.provider_message_id or "",
                    customer_phone=msg.from_number or "",
                    business_identifier=msg.phone_number_id or "",
                    body=(msg.text or "").strip(),
                    profile_name=msg.profile_name,
                    timestamp=msg.timestamp,
                    message_type=msg.message_type or "text",
                    skip_provider_outbound=True,
                    skip_ai_jobs=False,
                    business_display_phone=msg.display_phone_number,
                ),
                db=db,
            )
        except Exception:
            logger.exception(
                "meta_inbound process failed message_id=%s",
                (msg.provider_message_id or "")[:80],
            )
            continue

        if result.enqueue_classify and result.user_id and result.lead_id:
            try:
                from app.workers.queue import enqueue as _enq
                from app.workers import ai_tasks

                _enq(
                    ai_tasks.classify_latest_inbound,
                    result.user_id,
                    result.lead_id,
                    result.body or (msg.text or ""),
                    queue="default",
                )
            except Exception:
                logger.exception("meta classify enqueue failed")

        if result.enqueue_ai and result.user_id and result.lead_id:
            try:
                from app.workers.queue import enqueue
                from app.workers import tasks

                enqueue(
                    tasks.generate_and_send_ai_reply,
                    result.user_id,
                    result.lead_id,
                    provider="meta",
                    trigger_message_id=result.trigger_message_id,
                    queue="default",
                )
            except Exception:
                logger.exception("meta AI enqueue failed")

    for st in statuses:
        try:
            err_codes = [
                e.get("code") for e in (st.errors or []) if isinstance(e, dict) and "code" in e
            ]
            logger.info(
                "meta_status provider=%s message_id=%s status=%s recipient_id=%s "
                "phone_number_id=%s timestamp=%s error_codes=%s",
                st.provider,
                st.provider_message_id,
                st.status,
                st.recipient_id,
                st.phone_number_id,
                st.timestamp,
                err_codes,
            )
            await apply_meta_status_update(
                provider_message_id=st.provider_message_id,
                status_raw=st.status,
                errors=st.errors,
                phone_number_id=st.phone_number_id,
                db=db,
            )
        except Exception:
            logger.exception(
                "meta_status apply failed message_id=%s",
                (st.provider_message_id or "")[:80],
            )
            continue

    if not messages and not statuses:
        logger.info("meta_webhook received payload with no messages/statuses object=%s", payload.get("object"))

    return {"ok": True, "messages": len(messages), "statuses": len(statuses)}
