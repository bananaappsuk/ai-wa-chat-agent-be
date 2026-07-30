from typing import Optional
import json
import logging
from urllib.parse import urlparse
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from app.config import settings


logger = logging.getLogger(__name__)

_client_instance: Client | None = None

# Twilio cannot reach these hosts (error 21609). Sending them as StatusCallback
# causes the entire outbound message create to fail.
_NON_PUBLIC_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


def _client() -> Client:
    global _client_instance
    if _client_instance is None:
        _client_instance = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
    return _client_instance


_validator_instance: RequestValidator | None = None


def _validator() -> RequestValidator:
    global _validator_instance
    if _validator_instance is None:
        _validator_instance = RequestValidator(settings.TWILIO_AUTH_TOKEN)
    return _validator_instance


def to_whatsapp(num: str) -> str:
    """Provider boundary: E.164 → Twilio whatsapp: address."""
    from app.services.phone_norm import normalize_e164

    e164 = normalize_e164(num)
    if not e164:
        raise ValueError("Invalid phone number")
    return f"whatsapp:{e164}"


def from_whatsapp(num: str) -> str:
    from app.services.phone_norm import normalize_e164

    return normalize_e164(num) or num.replace("whatsapp:", "").strip()


def is_public_callback_url(url: str) -> bool:
    """True when URL is an absolute http(s) URL Twilio can accept (not localhost)."""
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    host = (parsed.hostname or "").lower()
    if not host or host in _NON_PUBLIC_HOSTS or host.endswith(".local"):
        return False
    return True


def resolve_status_callback(explicit: Optional[str] = None) -> Optional[str]:
    """Return a status callback URL, or None when none is configured / not public."""
    if explicit is not None:
        url = explicit.strip() or None
    else:
        url = settings.twilio_status_callback_url
        if not url and settings.APP_ENV not in ("dev", "test", "local"):
            logger.warning(
                "PUBLIC_BASE_URL is unset; Twilio status_callback will be omitted "
                "(APP_ENV=%s). Delivery status updates will not be received.",
                settings.APP_ENV,
            )
    if not url:
        return None
    if not is_public_callback_url(url):
        logger.warning(
            "Omitting Twilio status_callback because URL is not publicly reachable: %s",
            url,
        )
        return None
    return url


def send_whatsapp(
    to: str,
    body: Optional[str] = None,
    media_url: Optional[str] = None,
    status_callback: Optional[str] = None,
    content_sid: Optional[str] = None,
    content_variables: Optional[dict] = None,
) -> dict:
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise RuntimeError("Twilio credentials not configured")
    kwargs: dict = {
        "to": to_whatsapp(to),
    }
    msid = (settings.TWILIO_MESSAGING_SERVICE_SID or "").strip()
    if msid:
        kwargs["messaging_service_sid"] = msid
    else:
        from_num = (settings.TWILIO_WHATSAPP_FROM or "").strip()
        if not from_num:
            raise RuntimeError("TWILIO_WHATSAPP_FROM or TWILIO_MESSAGING_SERVICE_SID is required")
        kwargs["from_"] = from_num if from_num.startswith("whatsapp:") else to_whatsapp(from_num)
    if content_sid:
        kwargs["content_sid"] = content_sid
        if content_variables:
            kwargs["content_variables"] = json.dumps(content_variables)
    else:
        if media_url:
            kwargs["media_url"] = [media_url]
            if body:
                kwargs["body"] = body[:1600]
        else:
            if not body:
                raise ValueError("Message body is required when content_sid/media_url is not set")
            kwargs["body"] = body[:1600]
    callback = resolve_status_callback(status_callback)
    if callback:
        kwargs["status_callback"] = callback
    msg = _client().messages.create(**kwargs)
    return {"sid": msg.sid, "status": msg.status}


def fetch_message_status(sid: str) -> Optional[dict]:
    """Best-effort Twilio message status lookup for reconciliation."""
    if not sid or not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return None
    msg = _client().messages(sid).fetch()
    return {
        "sid": msg.sid,
        "status": msg.status,
        "error_code": getattr(msg, "error_code", None),
        "error_message": getattr(msg, "error_message", None),
    }


def get_content_template_info(content_sid: str) -> dict:
    """
    Fetch Twilio Content Template metadata + WhatsApp approval status.
    WhatsApp out-of-session sends require whatsapp.status == 'approved'.
    Local DB 'approved' alone is not enough (that only mirrors our app flag).
    """
    from app.services.whatsapp_template_approval import normalize_whatsapp_approval_status

    sid = (content_sid or "").strip()
    if not sid:
        return {
            "content_sid": None,
            "whatsapp_status": None,
            "whatsapp_status_raw": None,
            "body": None,
            "friendly_name": None,
            "whatsapp_category": None,
            "language": None,
            "business_initiated": None,
            "user_initiated": None,
            "provider": "twilio_content",
            "variables": {},
        }
    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise RuntimeError("Twilio credentials not configured")
    client = _client()
    content = client.content.v1.contents(sid).fetch()
    body = None
    types = getattr(content, "types", None) or {}
    if isinstance(types, dict):
        text = types.get("twilio/text") or {}
        if isinstance(text, dict):
            body = (text.get("body") or "").strip() or None
        if not body:
            # Common WhatsApp template type keys
            for key in ("twilio/quick-reply", "whatsapp/card", "twilio/media"):
                block = types.get(key) or {}
                if isinstance(block, dict) and (block.get("body") or block.get("title")):
                    body = (block.get("body") or block.get("title") or "").strip() or None
                    if body:
                        break

    wa_status_raw = None
    wa_category = None
    business_initiated = None
    user_initiated = None
    try:
        approvals = client.content.v1.contents(sid).approval_fetch().fetch()
        wa = getattr(approvals, "whatsapp", None) or {}
        if isinstance(wa, dict):
            wa_status_raw = (wa.get("status") or "").strip() or None
            wa_category = (wa.get("category") or "").strip() or None
            # Some Twilio payloads expose allow flags under nested keys
            bi = wa.get("allow_category_change")  # not BI — keep None unless explicit
            if "business_initiated" in wa:
                business_initiated = bool(wa.get("business_initiated"))
            if "user_initiated" in wa:
                user_initiated = bool(wa.get("user_initiated"))
            _ = bi
        elif wa is not None:
            wa_status_raw = (getattr(wa, "status", None) or "").strip() or None
            wa_category = (getattr(wa, "category", None) or "").strip() or None
    except Exception as exc:
        logger.warning(
            "Could not fetch WhatsApp approval for content_sid=%s err=%s",
            sid[:12],
            type(exc).__name__,
        )

    language = getattr(content, "language", None) or None
    friendly = getattr(content, "friendly_name", None)
    wa_norm = normalize_whatsapp_approval_status(wa_status_raw) if wa_status_raw else "unknown"
    if business_initiated is None:
        business_initiated = wa_norm == "approved"

    return {
        "content_sid": sid,
        "whatsapp_status": wa_norm,
        "whatsapp_status_raw": (wa_status_raw or "").lower() or None,
        "body": body,
        "friendly_name": friendly,
        "whatsapp_category": wa_category,
        "category": wa_category,
        "language": language,
        "business_initiated": business_initiated,
        "user_initiated": user_initiated,
        "provider": "twilio_content",
        "variables": getattr(content, "variables", None) or {},
    }


def assert_whatsapp_template_approved_for_out_of_session(
    content_sid: str,
    *,
    template_name: str | None = None,
) -> dict:
    """
    Raise if this Content SID cannot start a business-initiated WhatsApp chat.
    Unapproved/pending templates must not be sent outside the 24h window.
    Once Meta marks the template Approved, this check passes automatically
    (live status is fetched on every closed-window send — no code change needed).
    """
    from app.services.whatsapp_template_approval import (
        WhatsAppTemplateNotApprovedError,
        is_whatsapp_template_sendable,
        mask_content_sid,
    )

    info = get_content_template_info(content_sid)
    status = info.get("whatsapp_status")
    name = (template_name or info.get("friendly_name") or "").strip() or None
    if is_whatsapp_template_sendable(status):
        return info
    raise WhatsAppTemplateNotApprovedError(
        whatsapp_status=str(status or "unknown"),
        content_sid=info.get("content_sid") or content_sid,
        template_name=name,
    )


def log_template_send_gate(
    *,
    recipient_phone: str | None,
    window_open: bool,
    template_name: str | None,
    content_sid: str | None,
    approval_status: str | None,
    twilio_error_code: str | None = None,
    twilio_error_message: str | None = None,
    outcome: str,
) -> None:
    """Structured campaign template gate log — never includes secrets/tokens."""
    from app.services.whatsapp_template_approval import mask_content_sid

    logger.info(
        "campaign_template_gate outcome=%s recipient=%s window_open=%s template_name=%s "
        "content_sid=%s approval_status=%s twilio_error_code=%s twilio_error_message=%s",
        outcome,
        (recipient_phone or "")[-6:],  # last 6 digits only
        window_open,
        template_name or "",
        mask_content_sid(content_sid),
        approval_status or "",
        twilio_error_code or "",
        (twilio_error_message or "")[:200],
    )


def validate_signature(url: str, params: dict, signature: str) -> bool:
    # Bypass only when explicitly disabled (dev/test). Staging/production startup rejects disable.
    if not settings.twilio_validate_signatures:
        return True
    if not settings.TWILIO_AUTH_TOKEN or not signature:
        return False
    return _validator().validate(url, params, signature)
