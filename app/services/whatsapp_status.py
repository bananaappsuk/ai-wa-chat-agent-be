"""Safe WhatsApp / Twilio sender status for settings UI (no secrets)."""
from __future__ import annotations

from typing import Any

from app.config import settings
from app.services.twilio_service import is_public_callback_url


def detect_sender_type() -> str:
    from_num = (settings.TWILIO_WHATSAPP_FROM or "").strip().lower()
    ms = (settings.TWILIO_MESSAGING_SERVICE_SID or "").strip()
    if ms.startswith("MG"):
        return "messaging_service"
    if "14155238886" in from_num.replace(" ", ""):
        return "sandbox"
    if from_num.startswith("whatsapp:+") or from_num.startswith("+"):
        return "direct"
    if from_num:
        return "direct"
    return "none"


def whatsapp_status_payload() -> dict[str, Any]:
    sender_type = detect_sender_type()
    callback = settings.twilio_status_callback_url
    warnings: list[str] = []
    if settings.is_production_like and sender_type == "sandbox":
        warnings.append("Production is using the Twilio Sandbox sender — switch to a registered WhatsApp sender")
    if not (settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN):
        warnings.append("Twilio credentials are not configured")
    if sender_type == "none":
        warnings.append("No WhatsApp From number or Messaging Service SID configured")
    if not callback or not is_public_callback_url(callback):
        warnings.append("Status callback URL is missing or not publicly reachable")
    if not (settings.PUBLIC_BASE_URL or "").strip():
        warnings.append("PUBLIC_BASE_URL is not set")
    if settings.is_production_like and not settings.twilio_validate_signatures:
        warnings.append("Twilio signature validation is disabled")

    return {
        "environment": settings.APP_ENV,
        "sender_configured": sender_type != "none"
        and bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN),
        "sender_type": sender_type,
        "status_callback_configured": bool(callback and is_public_callback_url(callback)),
        "signature_validation_enabled": bool(settings.twilio_validate_signatures),
        "public_url_configured": bool((settings.PUBLIC_BASE_URL or "").strip()),
        "template_capability_configured": True,  # templates feature is available in-app
        "messaging_service_configured": bool(
            (settings.TWILIO_MESSAGING_SERVICE_SID or "").strip().startswith("MG")
        ),
        "production_ready": len(warnings) == 0 and settings.is_production_like,
        "warnings": warnings,
    }
