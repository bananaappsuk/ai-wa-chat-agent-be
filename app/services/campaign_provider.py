"""Resolve campaign/blast provider from the selected template (never env/inbound)."""
from __future__ import annotations

from typing import Any, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.db.mongo import get_db
from app.services.twilio_errors import classify_send_error


def stored_provider(doc: Optional[dict]) -> str:
    raw = ((doc or {}).get("provider") or "").strip().lower()
    if raw in ("meta", "twilio"):
        return raw
    return "twilio"


def classify_bulk_send_error(exc: BaseException, *, provider: str) -> str:
    """Classify campaign/blast send errors without Twilio-mapping Meta Graph failures."""
    if (provider or "").strip().lower() == "meta":
        from app.services.meta_whatsapp_service import MetaWhatsAppError
        from app.services.whatsapp_outbound import UnknownWhatsAppProviderError

        if isinstance(exc, UnknownWhatsAppProviderError):
            return "configuration_error"
        if isinstance(exc, MetaWhatsAppError):
            code = exc.status_code
            if code == 429:
                return "provider_rate_limited"
            if code is not None and 400 <= int(code) < 500:
                return "non_retryable"
            if code is not None and int(code) >= 500:
                return "retryable"
            return "retryable"
        if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
            return "retryable"
        try:
            import httpx

            if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.TransportError)):
                return "retryable"
        except Exception:
            pass
        # Consent/eligibility RuntimeErrors stay on the Meta path — never Twilio fallback.
        return classify_send_error(exc)
    return classify_send_error(exc)


async def peek_template(user_id: str, template_id: str) -> dict:
    if not ObjectId.is_valid(template_id):
        raise HTTPException(status_code=404, detail="Template not found")
    doc = await get_db().templates.find_one({"_id": ObjectId(template_id), "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Template not found")
    return doc


async def resolve_campaign_template(
    *,
    user_id: str,
    template_id: Optional[str],
    media_url: Optional[str] = None,
    content_mode: str = "template",
) -> dict[str, Any]:
    """
    Infer provider from the library template.

    Returns provider twilio|meta plus Twilio content_sid or Meta Graph identity.
    """
    tid = (template_id or "").strip() or None
    empty = {
        "provider": "twilio",
        "template_id": None,
        "content_sid": None,
        "meta_template_name": None,
        "meta_language_code": None,
    }
    if not tid:
        return empty

    peek = await peek_template(user_id, tid)
    is_meta = (peek.get("provider") or "") == "meta"

    # AI Agent campaigns on Meta: the AI personalises the approved template's declared
    # variables (Meta forbids free-form business-initiated sends, so template-variable
    # generation is the supported path). Media on Meta campaigns is still unsupported.

    if is_meta:
        if (media_url or "").strip():
            raise HTTPException(
                status_code=400,
                detail="Media sending is not supported for Meta WhatsApp campaigns or blasts.",
            )
        from app.routes.templates import get_sendable_meta_template

        tmpl = await get_sendable_meta_template(user_id, tid)
        return {
            "provider": "meta",
            "template_id": str(tmpl["_id"]),
            "content_sid": None,
            "meta_template_name": (tmpl.get("meta_template_name") or "").strip() or None,
            "meta_language_code": (tmpl.get("meta_language_code") or "").strip() or None,
        }

    from app.routes.templates import get_approved_template

    if (media_url or "").strip():
        raise HTTPException(status_code=400, detail="Cannot attach media to template campaigns")
    tmpl = await get_approved_template(user_id, tid)
    return {
        "provider": "twilio",
        "template_id": str(tmpl["_id"]),
        "content_sid": tmpl.get("content_sid"),
        "meta_template_name": None,
        "meta_language_code": None,
    }
