"""Canonical E.164 phone normalisation for the whole application.

Twilio `whatsapp:` prefix is applied only at the provider boundary
(`twilio_service.to_whatsapp`).
"""
from __future__ import annotations

import re
from typing import Optional

# Digits and optional leading +. Strip other punctuation/spaces.
_NON_DIGIT_PLUS = re.compile(r"[^\d+]")
_DEFAULT_REGION_CC = {
    "GB": "44",
    "UK": "44",
    "US": "1",
    "CA": "1",
}


def normalize_e164(
    raw: Optional[str],
    *,
    default_region: str = "GB",
) -> Optional[str]:
    """
    Normalise a phone number to E.164 (+digits) or return None if invalid.

    Rules:
    - Strip `whatsapp:` prefix if present
    - Strip spaces and punctuation (keep digits and leading +)
    - National numbers starting with 0 use default_region country code (GB → +44)
    - Bare country code without + is accepted (e.g. 4477… → +4477…)
    - Already-normalised +E.164 is preserved
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    lower = s.lower()
    if lower.startswith("whatsapp:"):
        s = s.split(":", 1)[1].strip()

    s = _NON_DIGIT_PLUS.sub("", s)
    if not s:
        return None

    # Collapse multiple leading +
    if s.startswith("+"):
        digits = re.sub(r"\D", "", s)
        if len(digits) < 8 or len(digits) > 15:
            return None
        return f"+{digits}"

    digits = re.sub(r"\D", "", s)
    if not digits:
        return None

    cc = _DEFAULT_REGION_CC.get(default_region.upper(), "44")
    if digits.startswith("0"):
        # National trunk prefix → country code
        digits = cc + digits[1:]
    elif not digits.startswith(cc) and len(digits) <= 10:
        # Ambiguous short local number without country — treat as national under default region
        digits = cc + digits.lstrip("0")

    if len(digits) < 8 or len(digits) > 15:
        return None
    return f"+{digits}"


def phones_equal(a: Optional[str], b: Optional[str]) -> bool:
    na = normalize_e164(a)
    nb = normalize_e164(b)
    return bool(na and nb and na == nb)


def is_self_sender(from_phone: Optional[str], user: Optional[dict]) -> bool:
    """C7: True when an inbound message's From matches our own sending number.

    Guards against WhatsApp-loop scenarios (e.g. sandbox mis-configuration,
    forwarded webhooks) where the tenant's own number appears as the sender.
    """
    from_norm = normalize_e164(from_phone)
    if not from_norm:
        return False

    from app.config import settings

    if normalize_e164(settings.TWILIO_WHATSAPP_FROM) == from_norm:
        return True

    user = user or {}
    if normalize_e164(user.get("twilio_whatsapp_to")) == from_norm:
        return True

    return False
