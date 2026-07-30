"""Twilio / WhatsApp error classification for retries."""
from __future__ import annotations

import re
from typing import Literal, Optional

ErrorCategory = Literal[
    "retryable",
    "non_retryable",
    "consent_blocked",
    "window_closed",
    "configuration_error",
    "provider_rate_limited",
    "invalid_recipient",
    "template_error",
    "media_error",
    "authentication_error",
]

# Known Twilio WhatsApp / messaging codes
_CODE_MAP: dict[str, ErrorCategory] = {
    "21211": "invalid_recipient",
    "21614": "invalid_recipient",
    "21408": "invalid_recipient",
    "21610": "consent_blocked",  # unsubscribed recipient
    # 63016 = freeform outside window. Often means Content Template was not
    # Meta-approved, so Twilio treated the send as freeform.
    "63016": "template_error",
    "63024": "template_error",
    "63007": "template_error",
    "63032": "media_error",
    "20003": "authentication_error",
    "20429": "provider_rate_limited",
    "429": "provider_rate_limited",
}

_RETRYABLE_HINTS = (
    "timeout",
    "timed out",
    "connection",
    "temporarily",
    "unavailable",
    "rate limit",
    "429",
    "502",
    "503",
    "504",
    "econnreset",
    "server error",
)

_NON_RETRY_HINTS = (
    "blacklist",
    "opted out",
    "opt-out",
    "unsubscribed",
    "consent",
    "window closed",
    "not approved",
    "invalid",
    "missing phone",
    "no content_sid",
    "template",
    "configuration",
)


def extract_twilio_error_code(exc: BaseException | str) -> Optional[str]:
    text = str(exc)
    m = re.search(r"\b([0-9]{5,6})\b", text)
    if m:
        return m.group(1)
    # Twilio RestException often has .code
    code = getattr(exc, "code", None)
    if code is not None:
        return str(code)
    return None


def classify_send_error(exc: BaseException | str) -> ErrorCategory:
    # Structured Meta approval failures — never treat as window_closed
    from app.services.whatsapp_template_approval import (
        WhatsAppTemplateNotApprovedError,
        is_template_approval_error_code,
    )

    if isinstance(exc, WhatsAppTemplateNotApprovedError):
        return "template_error"
    code_attr = getattr(exc, "error_code", None)
    if is_template_approval_error_code(str(code_attr) if code_attr else None):
        return "template_error"

    code = extract_twilio_error_code(exc)
    if code and code in _CODE_MAP:
        return _CODE_MAP[code]
    text = str(exc).lower()
    if is_template_approval_error_code(text.strip()) or any(
        h in text
        for h in (
            "template_under_review",
            "template_pending",
            "template_rejected",
            "template_paused",
            "template_not_approved",
            "currently under review by meta",
            "business-initiated whatsapp messages cannot be sent",
        )
    ):
        return "template_error"
    # Avoid classifying Meta approval messages that mention "24-hour" as window_closed
    if "by meta" in text or "content template is not approved" in text:
        return "template_error"
    if "window" in text or "24-hour" in text or "24 hour" in text:
        return "window_closed"
    if any(h in text for h in ("consent", "opt-out", "opted out", "blacklist", "unsubscribed")):
        return "consent_blocked"
    for hint in _NON_RETRY_HINTS:
        if hint in text:
            if "template" in hint or "content_sid" in hint or "approved" in hint:
                return "template_error"
            if "invalid" in hint or "phone" in hint:
                return "invalid_recipient"
            if "config" in hint:
                return "configuration_error"
            return "non_retryable"
    for hint in _RETRYABLE_HINTS:
        if hint in text:
            if "429" in hint or "rate" in hint:
                return "provider_rate_limited"
            return "retryable"
    return "retryable"


def is_retryable_category(category: ErrorCategory) -> bool:
    return category in ("retryable", "provider_rate_limited")
