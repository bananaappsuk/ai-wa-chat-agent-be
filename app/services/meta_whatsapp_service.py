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
    profile_name: str | None = None
    display_phone_number: str | None = None
    media_id: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    sha256: str | None = None
    voice: bool = False
    kind: str | None = None


@dataclass(frozen=True)
class InboundWhatsAppStatus:
    provider: str
    provider_message_id: str
    status: str
    recipient_id: str | None = None
    timestamp: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)
    phone_number_id: str | None = None


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


def assert_meta_send_configured(user: Optional[dict] = None) -> None:
    from app.services.meta_credentials import MetaCredentialsError, get_meta_credentials_for_user

    try:
        get_meta_credentials_for_user(user)
    except MetaCredentialsError as exc:
        raise MetaWhatsAppError(str(exc)) from exc


def resolve_send_phone_number_id(
    phone_number_id: Optional[str] = None,
    *,
    user: Optional[dict] = None,
) -> str:
    """Tenant PNID from resolver. Does not use env PNID as a production equality gate."""
    from app.services.meta_credentials import MetaCredentialsError, get_meta_credentials_for_user

    try:
        creds = get_meta_credentials_for_user(user)
    except MetaCredentialsError as exc:
        raise MetaWhatsAppError(str(exc)) from exc
    explicit = (phone_number_id or "").strip()
    if explicit and explicit != creds.phone_number_id:
        raise MetaWhatsAppError("Tenant meta_phone_number_id does not match send credentials")
    return creds.phone_number_id


def send_text(
    *,
    to: str,
    text: str,
    user: Optional[dict] = None,
    phone_number_id: Optional[str] = None,
) -> MetaSendResult:
    """
    Send a plain-text WhatsApp message via Meta Cloud API.

    POST https://graph.facebook.com/{version}/{phone-number-id}/messages
    """
    from app.services.meta_credentials import MetaCredentialsError, get_meta_credentials_for_user

    try:
        creds = get_meta_credentials_for_user(user)
    except MetaCredentialsError as exc:
        raise MetaWhatsAppError(str(exc)) from exc
    body_text = (text or "").strip()
    if not body_text:
        raise MetaWhatsAppError("Message text is required")
    if len(body_text) > 4096:
        raise MetaWhatsAppError("Message text exceeds WhatsApp limit (4096 characters)")

    to_digits = _to_meta_digits(to)
    explicit = (phone_number_id or "").strip()
    if explicit and explicit != creds.phone_number_id:
        raise MetaWhatsAppError("Tenant meta_phone_number_id does not match send credentials")
    phone_number_id = creds.phone_number_id
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
        "Authorization": f"Bearer {creds.access_token}",
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
        from app.services.meta_credentials import maybe_mark_meta_auth_death

        maybe_mark_meta_auth_death(user, http_status=resp.status_code, error=err)
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


def send_template(
    *,
    to: str,
    name: str,
    language_code: str,
    components: list[dict[str, Any]] | None = None,
    user: Optional[dict] = None,
    phone_number_id: Optional[str] = None,
) -> MetaSendResult:
    """
    Send a WhatsApp template via Meta Cloud API.

    POST https://graph.facebook.com/{version}/{phone-number-id}/messages
    """
    tmpl_name = (name or "").strip()
    lang = (language_code or "").strip()
    if not tmpl_name:
        raise MetaWhatsAppError("Template name is required")
    if not lang:
        raise MetaWhatsAppError("Template language is required")

    to_digits = _to_meta_digits(to)
    from app.services.meta_credentials import MetaCredentialsError, get_meta_credentials_for_user

    try:
        creds = get_meta_credentials_for_user(user)
    except MetaCredentialsError as exc:
        raise MetaWhatsAppError(str(exc)) from exc
    explicit = (phone_number_id or "").strip()
    if explicit and explicit != creds.phone_number_id:
        raise MetaWhatsAppError("Tenant meta_phone_number_id does not match send credentials")
    phone_number_id = creds.phone_number_id
    version = (settings.META_GRAPH_VERSION or "v21.0").strip().lstrip("/")
    url = f"https://graph.facebook.com/{version}/{phone_number_id}/messages"
    template_obj: dict[str, Any] = {
        "name": tmpl_name,
        "language": {"code": lang},
    }
    if components:
        template_obj["components"] = components
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to_digits,
        "type": "template",
        "template": template_obj,
    }
    headers = {
        "Authorization": f"Bearer {creds.access_token}",
        "Content-Type": "application/json",
    }
    timeout = max(5.0, float(settings.META_HTTP_TIMEOUT_SECONDS or 30.0))

    logger.info(
        "meta_send_template phone_number_id=%s to=%s name=%s language=%s graph_version=%s",
        phone_number_id,
        to_digits,
        tmpl_name,
        lang,
        version,
    )

    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        logger.warning("meta_send_template timeout to=%s", to_digits)
        raise MetaWhatsAppError("Meta Graph API request timed out") from exc
    except httpx.HTTPError as exc:
        logger.warning("meta_send_template http_error type=%s", type(exc).__name__)
        raise MetaWhatsAppError(f"Meta Graph API request failed: {type(exc).__name__}") from exc

    try:
        data = resp.json()
    except Exception:
        data = {"raw": (resp.text or "")[:500]}

    if resp.is_error:
        err = data.get("error") if isinstance(data, dict) else None
        msg = "Meta Graph API template send failed"
        if isinstance(err, dict):
            msg = str(err.get("message") or msg)
        logger.warning(
            "meta_send_template failed status=%s to=%s error=%s",
            resp.status_code,
            to_digits,
            (msg or "")[:200],
        )
        from app.services.meta_credentials import maybe_mark_meta_auth_death

        maybe_mark_meta_auth_death(user, http_status=resp.status_code, error=err)
        raise MetaWhatsAppError(msg, status_code=resp.status_code, details=err or data)

    message_id = None
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list) and messages:
            first = messages[0]
            if isinstance(first, dict):
                message_id = first.get("id")

    logger.info(
        "meta_send_template ok to=%s provider_message_id=%s",
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


def _media_block(msg: dict[str, Any], msg_type: str) -> dict[str, Any] | None:
    """Normalize Meta media object fields. Does not fetch Graph."""
    key = msg_type if msg_type != "voice" else "audio"
    if msg_type == "sticker":
        key = "sticker"
    block = msg.get(key)
    if msg_type == "voice" and not isinstance(block, dict):
        block = msg.get("voice")
    if not isinstance(block, dict):
        return None
    media_id = str(block.get("id") or "").strip() or None
    if not media_id:
        return None
    kind = "audio" if msg_type in ("audio", "voice") else ("image" if msg_type == "sticker" else msg_type)
    caption = block.get("caption")
    caption_s = str(caption).strip() if caption is not None and str(caption).strip() else None
    filename = str(block.get("filename") or "").strip() or None
    mime = str(block.get("mime_type") or "").strip() or None
    sha = str(block.get("sha256") or "").strip() or None
    voice = bool(block.get("voice")) or msg_type == "voice"
    return {
        "media_id": media_id,
        "mime_type": mime,
        "filename": filename,
        "caption": caption_s,
        "sha256": sha,
        "voice": voice,
        "kind": kind,
    }


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
            display_raw = metadata.get("display_phone_number")
            display_phone = str(display_raw).strip() if display_raw else None
            name_by_wa: dict[str, str] = {}
            for contact in value.get("contacts") or []:
                if not isinstance(contact, dict):
                    continue
                wa_id = str(contact.get("wa_id") or "").strip()
                profile = contact.get("profile") if isinstance(contact.get("profile"), dict) else {}
                cname = profile.get("name")
                if wa_id and cname:
                    name_by_wa[wa_id] = str(cname)
            for msg in value.get("messages") or []:
                if not isinstance(msg, dict):
                    continue
                msg_type = str(msg.get("type") or "unknown")
                text_body = None
                media = None
                if msg_type == "text" and isinstance(msg.get("text"), dict):
                    text_body = msg["text"].get("body")
                    if text_body is not None:
                        text_body = str(text_body)
                elif msg_type in ("image", "document", "audio", "video", "voice", "sticker"):
                    media = _media_block(msg, msg_type)
                    text_body = (media.get("caption") if media else None) or ""
                from_raw = str(msg.get("from") or "")
                from_norm = normalize_meta_phone(from_raw) or from_raw
                profile_name = name_by_wa.get(from_raw) or name_by_wa.get(from_raw.lstrip("+"))
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
                        profile_name=profile_name,
                        display_phone_number=display_phone,
                        media_id=(media or {}).get("media_id"),
                        mime_type=(media or {}).get("mime_type"),
                        filename=(media or {}).get("filename"),
                        sha256=(media or {}).get("sha256"),
                        voice=bool((media or {}).get("voice")),
                        kind=(media or {}).get("kind"),
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
            metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
            phone_number_id = str(metadata.get("phone_number_id") or "").strip() or None
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
                        phone_number_id=phone_number_id,
                    )
                )
    return out


def inbound_to_dict(msg: InboundWhatsAppMessage) -> dict[str, Any]:
    return asdict(msg)


def status_to_dict(st: InboundWhatsAppStatus) -> dict[str, Any]:
    return asdict(st)
