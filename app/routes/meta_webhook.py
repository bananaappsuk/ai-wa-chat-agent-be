"""Meta WhatsApp Cloud API webhooks (Phase 1 POC — verify + log only)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.services import meta_whatsapp_service

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

    Phase 1: verify signature, parse, log safe fields, return 200.
    Does not write Mongo or enqueue AI/campaigns.
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

    for msg in messages:
        text_len = len(msg.text) if msg.text is not None else 0
        # Avoid logging full message bodies in production-like; length is enough for POC proof.
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

    for st in statuses:
        err_codes = [
            e.get("code") for e in (st.errors or []) if isinstance(e, dict) and "code" in e
        ]
        logger.info(
            "meta_status provider=%s message_id=%s status=%s recipient_id=%s "
            "timestamp=%s error_codes=%s",
            st.provider,
            st.provider_message_id,
            st.status,
            st.recipient_id,
            st.timestamp,
            err_codes,
        )

    if not messages and not statuses:
        logger.info("meta_webhook received payload with no messages/statuses object=%s", payload.get("object"))

    return {"ok": True, "messages": len(messages), "statuses": len(statuses)}
