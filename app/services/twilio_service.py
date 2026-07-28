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


def validate_signature(url: str, params: dict, signature: str) -> bool:
    # Bypass only when explicitly disabled (dev/test). Staging/production startup rejects disable.
    if not settings.twilio_validate_signatures:
        return True
    if not settings.TWILIO_AUTH_TOKEN or not signature:
        return False
    return _validator().validate(url, params, signature)
