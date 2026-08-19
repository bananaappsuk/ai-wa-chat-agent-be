"""Meta WhatsApp Cloud API helpers (Phase 1 POC).

Additive to Twilio. Does not replace twilio_service. Secrets stay server-side.
Never log META_ACCESS_TOKEN or META_APP_SECRET.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_DIGITS = re.compile(r"\D+")


class MetaWhatsAppError(Exception):
    """Raised when Meta Graph API send fails or config is incomplete."""

    def __init__(self, message: str, *, status_code: int | None = None, details: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


@dataclass(frozen=True)
class InboundWhatsAppMessage:
    provider: str
    provider_message_id: str
    from_number: str
    phone_number_id: str
    message_type: str
    text: str | None = None
    timestamp: str | None = None
    raw_type: str | None = None


@dataclass(frozen=True)
class InboundWhatsAppStatus:
    provider: str
    provider_message_id: str
    status: str
    recipient_id: str | None = None
    timestamp: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MetaSendResult:
    provider: str
    provider_message_id: str | None
    phone_number_id: str
    to: str
    raw: dict[str, Any]


def _to_meta_digits(phone: str) -> str:
    """Meta Cloud API expects recipient as digits (country code, no '+')."""
    from app.services.phone_norm import normalize_e164

    e164 = normalize_e164(phone)
    if not e164:
        raise MetaWhatsAppError("Invalid destination phone number")
    return e164.lstrip("+")


def normalize_meta_phone(raw: Optional[str]) -> Optional[str]:
    """Normalize Meta webhook `from` / recipient ids to E.164 when possible."""
    from app.services.phone_norm import normalize_e164

    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if s.startswith("+"):
        return normalize_e164(s)
    digits = _DIGITS.sub("", s)
    if not digits:
        return None
    return normalize_e164(f"+{digits}")


def assert_meta_send_configured() -> None:
    if not (settings.META_ACCESS_TOKEN or "").strip():
        raise MetaWhatsAppError("META_ACCESS_TOKEN is not configured")
    if not (settings.META_PHONE_NUMBER_ID or "").strip():
        raise MetaWhatsAppError("META_PHONE_NUMBER_ID is not configured")


def send_text(*, to: str, text: str) -> MetaSendResult:
    """
    Send a plain-text WhatsApp message via Meta Cloud API.

    POST https://graph.facebook.com/{version}/{phone-number-id}/messages
    """
    assert_meta_send_configured()
    body_text = (text or "").strip()
    if not body_text:
        raise MetaWhatsAppError("Message text is required")
    if len(body_text) > 4096:
        raise MetaWhatsAppError("Message text exceeds WhatsApp limit (4096 characters)")

    to_digits = _to_meta_digits(to)
    phone_number_id = settings.META_PHONE_NUMBER_ID.strip()
    version = (settings.META_GRAPH_VERSION or "v21.0").strip().lstrip("/")
    url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_digits,
        "type": "text",
        "text": {"body": body_text},
    }
    headers = {
        "Authorization": f"Bearer {settings.META_ACCESS_TOKEN.strip()}",
        "Content-Type": "application/json",
    }
    timeout = max(5.0, float(settings.META_HTTP_TIMEOUT_SECONDS or 30.0))

    logger.info(
        "meta_send_text phone_number_id=%s to=%s text_len=%s graph_version=%s",
        phone_number_id,
        to_digits,
        len(body_text),
        version,
    )

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        logger.warning("meta_send_text timeout to=%s", to_digits)
        raise MetaWhatsAppError("Meta Graph API request timed out") from exc
    except httpx.HTTPError as exc:
        logger.warning("meta_send_text http_error type=%s", type(exc).__name__)
        raise MetaWhatsAppError(f"Meta Graph API request failed: {type(exc).__name__}") from exc

    try:
        data = resp.json()
    except Exception:
        data = {"raw": (resp.text or "")[:500]}

    if resp.is_error:
        # Never include Authorization / token. Meta error payloads are usually safe.
        err = data.get("error") if isinstance(data, dict) else None
        msg = "Meta Graph API send failed"
        if isinstance(err, dict):
            msg = str(err.get("message") or msg)
        logger.warning(
            "meta_send_text failed status=%s to=%s error=%s",
            resp.status_code,
            to_digits,
            (msg or "")[:200],
        )
        raise MetaWhatsAppError(msg, status_code=resp.status_code, details=err or data)

    message_id = None
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict):
                message_id = first.get("id")

    logger.info(
        "meta_send_text ok to=%s provider_message_id=%s",
        to_digits,
        message_id,
    )
    return MetaSendResult(
        provider="meta",
        provider_message_id=str(message_id) if message_id else None,
        phone_number_id=phone_number_id,
        to=to_digits,
        raw=data if isinstance(data, dict) else {"response": data},
    )


def verify_webhook_signature(*, raw_body: bytes, signature_header: str | None) -> bool:
    """
    Validate Meta X-Hub-Signature-256 (sha256=<hex>) against META_APP_SECRET and raw body.
    """
    if not settings.META_WEBHOOK_VALIDATE_SIGNATURE:
        logger.warning(
            "meta_webhook signature validation disabled (META_WEBHOOK_VALIDATE_SIGNATURE=false)"
        )
        return True

    secret = (settings.META_APP_SECRET or "").strip()
    if not secret:
        logger.warning("meta_webhook rejected: META_APP_SECRET not configured")
        return False

    header = (signature_header or "").strip()
    if not header.startswith("sha256="):
        return False
    provided = header[7:].strip()
    if not provided:
        return False

    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, provided)


def verify_webhook_subscribe(*, mode: str | None, token: str | None) -> bool:
    expected = (settings.META_WEBHOOK_VERIFY_TOKEN or "").strip()
    if not expected:
        logger.warning("meta_webhook verify rejected: META_WEBHOOK_VERIFY_TOKEN not configured")
        return False
    if (mode or "").strip() != "subscribe":
        return False
    provided = (token or "").strip()
    if len(provided) != len(expected):
        return False
    return hmac.compare_digest(provided, expected)


def parse_inbound_messages(payload: dict[str, Any]) -> list[InboundWhatsAppMessage]:
    """Extract normalized inbound messages from a Meta Cloud API webhook JSON body."""
    out: list[InboundWhatsAppMessage] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            phone_number_id = str(metadata.get("phone_number_id") or "")
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                msg_type = str(msg.get("type") or "unknown")
                text_body = None
                if msg_type == "text" and isinstance(msg.get("text"), dict):
                    text_body = msg["text"].get("body")
                    if text_body is not None:
                        text_body = str(text_body)
                from_raw = str(msg.get("from") or "")
                from_norm = normalize_meta_phone(from_raw) or from_raw
                out.append(
                    InboundWhatsAppMessage(
                        provider="meta",
                        provider_message_id=str(msg.get("id") or ""),
                        from_number=from_norm,
                        phone_number_id=phone_number_id,
                        message_type=msg_type,
                        text=text_body,
                        timestamp=str(msg.get("timestamp")) if msg.get("timestamp") is not None else None,
                        raw_type=msg_type,
                    )
                )
    return out


def parse_status_updates(payload: dict[str, Any]) -> list[InboundWhatsAppStatus]:
    """Extract delivery status updates from a Meta Cloud API webhook JSON body."""
    out: list[InboundWhatsAppStatus] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            for st in value.get("statuses") or []:
                if not isinstance(st, dict):
                    continue
                errors = st.get("errors") if isinstance(st.get("errors"), list) else []
                safe_errors = [e for e in errors if isinstance(e, dict)]
                out.append(
                    InboundWhatsAppStatus(
                        provider="meta",
                        provider_message_id=str(st.get("id") or ""),
                        status=str(st.get("status") or ""),
                        recipient_id=str(st["recipient_id"]) if st.get("recipient_id") is not None else None,
                        timestamp=str(st.get("timestamp")) if st.get("timestamp") is not None else None,
                        errors=safe_errors,
                    )
                )
    return out


def inbound_to_dict(msg: InboundWhatsAppMessage) -> dict[str, Any]:
    return asdict(msg)


def status_to_dict(st: InboundWhatsAppStatus) -> dict[str, Any]:
    return asdict(st)
