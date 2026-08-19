"""Text-only WhatsApp send dispatcher (Phase 2B). Media/templates stay provider-native."""
from __future__ import annotations

from typing import Any, Optional

from app.services import meta_whatsapp_service, twilio_service
from app.services.meta_whatsapp_service import MetaWhatsAppError


class UnknownWhatsAppProviderError(ValueError):
    """Raised when an AI/outbound job names a provider that is not twilio or meta."""


def send_whatsapp_text(
    *,
    provider: str,
    to: str,
    text: str,
    user: Optional[dict] = None,
) -> dict[str, Any]:
    """
    Send a free-form text message on the given provider.

    Never consults WHATSAPP_PROVIDER env. Never falls back across providers.
    """
    prov = (provider or "").strip().lower()
    if prov == "twilio":
        result = twilio_service.send_whatsapp(to, body=text)
        sid = result.get("sid")
        return {
            "provider": "twilio",
            "provider_message_id": sid,
            "status": result.get("status") or "sent",
        }
    if prov == "meta":
        pnid = None
        if user:
            raw = user.get("meta_phone_number_id")
            pnid = str(raw).strip() if raw else None
        result = meta_whatsapp_service.send_text(to=to, text=text, phone_number_id=pnid or None)
        return {
            "provider": "meta",
            "provider_message_id": result.provider_message_id,
            "status": "sent",
        }
    raise UnknownWhatsAppProviderError(f"Unknown WhatsApp provider: {prov or '(empty)'}")


__all__ = ["send_whatsapp_text", "UnknownWhatsAppProviderError", "MetaWhatsAppError"]
