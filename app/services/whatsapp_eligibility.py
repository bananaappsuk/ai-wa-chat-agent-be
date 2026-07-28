"""Central WhatsApp send eligibility / outbound policy."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal, Optional

from app.config import settings
from app.services.whatsapp_consent import apply_consent_defaults
from app.services.whatsapp_window import is_whatsapp_window_open

MessagePurpose = Literal[
    "conversational",
    "transactional",
    "marketing",
    "support",
    "campaign",
    "opt_out_confirmation",
]

MARKETING_PURPOSES = frozenset({"marketing", "campaign"})


@dataclass
class EligibilityResult:
    allowed: bool
    reason_code: str
    safe_message: str
    consent_status: str
    window_status: str  # open | closed | unknown
    blacklist_status: str  # blocked | clear
    template_required: bool = False
    sender_configured: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sender_ok() -> bool:
    has_creds = bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN)
    has_from = bool((settings.TWILIO_WHATSAPP_FROM or "").strip())
    has_ms = bool((settings.TWILIO_MESSAGING_SERVICE_SID or "").strip())
    return has_creds and (has_from or has_ms)


def _window_status(lead: Optional[dict]) -> str:
    if not lead:
        return "unknown"
    if is_whatsapp_window_open(lead):
        return "open"
    if lead.get("whatsapp_window_expires_at") or lead.get("last_inbound_at"):
        return "closed"
    return "unknown"


def get_whatsapp_send_eligibility(
    *,
    lead: Optional[dict],
    phone: Optional[str] = None,
    purpose: MessagePurpose = "conversational",
    has_template: bool = False,
    has_media: bool = False,
    blacklisted: Optional[bool] = None,
) -> EligibilityResult:
    """Reusable eligibility check for UI preview and send paths."""
    lead = apply_consent_defaults(dict(lead or {}))
    consent = (lead.get("whatsapp_consent_status") or "unknown").strip().lower()
    is_bl = bool(lead.get("blacklisted")) if blacklisted is None else bool(blacklisted)
    phone_val = (phone or lead.get("phone") or "").strip()
    window = _window_status(lead)
    sender = _sender_ok()

    if not sender:
        return EligibilityResult(
            False,
            "configuration_error",
            "WhatsApp sender is not configured",
            consent,
            window,
            "blocked" if is_bl else "clear",
            template_required=not has_template and window != "open",
            sender_configured=False,
        )

    if not phone_val or not phone_val.startswith("+"):
        return EligibilityResult(
            False,
            "invalid_recipient",
            "Valid E.164 phone number is required",
            consent,
            window,
            "blocked" if is_bl else "clear",
            sender_configured=True,
        )

    # One-shot STOP confirmation may send even when already opted out / blacklisted.
    if purpose == "opt_out_confirmation":
        return EligibilityResult(
            True,
            "ok",
            "Allowed",
            consent,
            window,
            "blocked" if is_bl else "clear",
            sender_configured=True,
        )

    if is_bl or consent == "opted_out":
        return EligibilityResult(
            False,
            "consent_blocked",
            "Recipient has opted out or is blacklisted",
            "opted_out" if consent == "opted_out" else consent,
            window,
            "blocked",
            sender_configured=True,
        )

    if purpose in MARKETING_PURPOSES:
        if consent != "opted_in":
            return EligibilityResult(
                False,
                "consent_required",
                "Marketing messages require explicit WhatsApp opt-in",
                consent,
                window,
                "clear",
                template_required=True,
                sender_configured=True,
            )
        if not has_template and window != "open":
            return EligibilityResult(
                False,
                "window_closed",
                "Outside the 24-hour window — use an approved template",
                consent,
                window,
                "clear",
                template_required=True,
                sender_configured=True,
            )
        return EligibilityResult(
            True,
            "ok",
            "Allowed",
            consent,
            window,
            "clear",
            template_required=not has_template and window != "open",
            sender_configured=True,
        )

    # conversational / transactional / support
    if has_template:
        return EligibilityResult(
            True,
            "ok",
            "Allowed",
            consent,
            window,
            "clear",
            sender_configured=True,
        )
    if window != "open":
        return EligibilityResult(
            False,
            "window_closed",
            "WhatsApp customer service window closed. Use an approved template.",
            consent,
            window,
            "clear",
            template_required=True,
            sender_configured=True,
        )
    return EligibilityResult(
        True,
        "ok",
        "Allowed",
        consent,
        window,
        "clear",
        sender_configured=True,
    )


def can_send_marketing(lead: Optional[dict], *, has_template: bool = False) -> EligibilityResult:
    return get_whatsapp_send_eligibility(
        lead=lead, purpose="marketing", has_template=has_template
    )


def can_send_transactional(
    lead: Optional[dict], *, has_template: bool = False
) -> EligibilityResult:
    return get_whatsapp_send_eligibility(
        lead=lead, purpose="transactional", has_template=has_template
    )


def assert_outbound_allowed(result: EligibilityResult) -> None:
    """Raise ValueError with safe_message when not allowed (for workers)."""
    if not result.allowed:
        raise ValueError(result.safe_message)
