import logging

from fastapi import APIRouter, Request, Response, HTTPException

from app.db.mongo import get_db
from app.services import twilio_service
from app.services.status_callback import apply_twilio_status_callback
from app.services.media import parse_inbound_media, media_fields_from_items
from app.services.inbound_whatsapp import InboundMessage, process_inbound_message
from app.workers.queue import enqueue
from app.workers import tasks

router = APIRouter(tags=["webhook"])
logger = logging.getLogger(__name__)


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
    twilio_sid = params.get("MessageSid") or params.get("SmsMessageSid") or ""
    from_raw = params.get("From", "")
    to_raw = params.get("To", "")
    body = (params.get("Body") or "").strip()
    profile_name = params.get("ProfileName") or None
    if not from_raw:
        return Response(content="<Response/>", media_type="application/xml")

    from_phone = twilio_service.from_whatsapp(from_raw)
    to_phone = twilio_service.from_whatsapp(to_raw) if to_raw else ""
    media_items = parse_inbound_media(params)
    media_meta = media_fields_from_items(media_items, body)

    result = await process_inbound_message(
        InboundMessage(
            provider="twilio",
            provider_message_id=str(twilio_sid).strip(),
            customer_phone=from_phone,
            business_identifier=to_phone or "",
            body=body,
            profile_name=profile_name,
            timestamp=None,
            message_type=str(media_meta.get("message_type") or "text"),
            skip_provider_outbound=False,
            skip_ai_jobs=False,
            media_meta=media_meta,
        ),
        db=get_db(),
    )

    if result.enqueue_welcome and result.user_id and result.lead_id:
        try:
            enqueue(tasks.send_welcome_and_terms, result.user_id, result.lead_id, queue="high")
        except Exception:
            logger.exception("Failed to enqueue welcome/terms send")

    if result.enqueue_classify and result.user_id and result.lead_id:
        try:
            from app.workers.queue import enqueue as _enq
            from app.workers import ai_tasks

            _enq(
                ai_tasks.classify_latest_inbound,
                result.user_id,
                result.lead_id,
                result.body or body or "",
                queue="default",
            )
        except Exception:
            logger.exception("classify enqueue failed")

    if result.enqueue_ai and result.user_id and result.lead_id:
        enqueue(
            tasks.generate_and_send_ai_reply,
            result.user_id,
            result.lead_id,
            provider="twilio",
            trigger_message_id=result.trigger_message_id,
            queue="default",
        )

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
