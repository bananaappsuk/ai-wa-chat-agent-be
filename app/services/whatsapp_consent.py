"""WhatsApp consent tracking and keyword opt-out/in helpers."""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

from app.config import settings
from app.models.common import utcnow

ConsentStatus = Literal["unknown", "pending", "opted_in", "opted_out"]
ConsentSource = Literal[
    "inbound_message",
    "website_form",
    "manual",
    "import",
    "campaign",
    "api",
    "other",
    "keyword_optout",
    "keyword_optin",
    "blacklist",
]

CONSENT_DEFAULTS = {
    "whatsapp_consent_status": "unknown",
    "whatsapp_consent_source": None,
    "whatsapp_consent_at": None,
    "whatsapp_consent_updated_at": None,
    "whatsapp_consent_proof": None,
    "whatsapp_opted_out_at": None,
    "whatsapp_opt_out_reason": None,
}

_PUNCT_STRIP = re.compile(r"^[\s\"'`.,!?;:()\[\]{}<>]+|[\s\"'`.,!?;:()\[\]{}<>]+$")


def apply_consent_defaults(doc: dict) -> dict:
    for k, v in CONSENT_DEFAULTS.items():
        doc.setdefault(k, v)
    return doc


def normalize_keyword_body(body: str) -> str:
    """Normalize inbound body for keyword matching (trimmed, de-punctuated, lower)."""
    raw = (body or "").strip()
    raw = _PUNCT_STRIP.sub("", raw)
    return raw.lower().strip()


def is_optout_keyword(body: str) -> bool:
    token = normalize_keyword_body(body)
    # Exact match on whole message (avoid false positives in long text)
    return token in settings.optout_keywords


def is_optin_keyword(body: str) -> bool:
    token = normalize_keyword_body(body)
    return token in settings.optin_keywords


def consent_update_fields(
    *,
    status: ConsentStatus,
    source: ConsentSource | str,
    proof: Optional[str] = None,
    reason: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> dict[str, Any]:
    now = utcnow()
    fields: dict[str, Any] = {
        "whatsapp_consent_status": status,
        "whatsapp_consent_source": source,
        "whatsapp_consent_updated_at": now,
        "updated_at": now,
    }
    if proof is not None:
        fields["whatsapp_consent_proof"] = str(proof)[:500]
    if changed_by is not None:
        fields["whatsapp_consent_changed_by"] = changed_by
    if status == "opted_in":
        fields["whatsapp_consent_at"] = now
        fields["whatsapp_opted_out_at"] = None
        fields["whatsapp_opt_out_reason"] = None
        fields["blacklisted"] = False
    elif status == "opted_out":
        fields["whatsapp_opted_out_at"] = now
        fields["whatsapp_opt_out_reason"] = (reason or "opted_out")[:200]
        fields["blacklisted"] = True
        fields["ai_paused"] = True
    return fields
