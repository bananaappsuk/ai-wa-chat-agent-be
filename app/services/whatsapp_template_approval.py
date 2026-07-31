"""WhatsApp Content Template Meta approval helpers (Twilio Content API).

Designed for Twilio Content Templates today; field names support future Meta /
multi-sender / multi-tenant adapters without changing campaign send paths.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Stable recipient / analytics reason codes (stored on campaign_recipients.error_code)
TEMPLATE_UNDER_REVIEW = "template_under_review"
TEMPLATE_PENDING = "template_pending"
TEMPLATE_REJECTED = "template_rejected"
TEMPLATE_PAUSED = "template_paused"
TEMPLATE_NOT_APPROVED = "template_not_approved"

STATUS_TO_ERROR_CODE: dict[str, str] = {
    "under_review": TEMPLATE_UNDER_REVIEW,
    "pending": TEMPLATE_PENDING,
    "unsubmitted": TEMPLATE_PENDING,
    "received": TEMPLATE_PENDING,
    "submitted": TEMPLATE_PENDING,
    "rejected": TEMPLATE_REJECTED,
    "paused": TEMPLATE_PAUSED,
    "disabled": TEMPLATE_PAUSED,
    "unknown": TEMPLATE_NOT_APPROVED,
}

STATUS_DISPLAY: dict[str, str] = {
    "approved": "Approved",
    "under_review": "Under Review",
    "pending": "Pending",
    "rejected": "Rejected",
    "paused": "Paused",
    "unsubmitted": "Pending",
    "received": "Pending",
    "submitted": "Pending",
    "disabled": "Paused",
    "unknown": "Unknown",
}


class WhatsAppTemplateNotApprovedError(RuntimeError):
    """Raised when a business-initiated send is blocked by Meta approval status."""

    def __init__(
        self,
        *,
        whatsapp_status: str,
        content_sid: str,
        template_name: Optional[str] = None,
        twilio_error_code: Optional[str] = None,
        twilio_error_message: Optional[str] = None,
    ) -> None:
        self.whatsapp_status = normalize_whatsapp_approval_status(whatsapp_status)
        self.content_sid = content_sid or ""
        self.template_name = (template_name or "").strip() or None
        self.error_code = STATUS_TO_ERROR_CODE.get(self.whatsapp_status, TEMPLATE_NOT_APPROVED)
        self.twilio_error_code = twilio_error_code
        self.twilio_error_message = twilio_error_message
        super().__init__(self.user_message())

    def display_status(self) -> str:
        return STATUS_DISPLAY.get(self.whatsapp_status, self.whatsapp_status.replace("_", " ").title())

    def user_message(self) -> str:
        name = self.template_name or "selected template"
        label = self.display_status()
        return (
            f"Template '{name}' is currently {label} by Meta.\n\n"
            "Business-initiated WhatsApp messages cannot be sent until the template is approved."
        )


def normalize_whatsapp_approval_status(raw: Optional[str]) -> str:
    """Normalize Twilio/Meta status strings to a small canonical set.

    Twilio Console often labels ``pending`` / ``received`` as "Under Review".
    """
    s = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not s:
        return "unknown"
    aliases = {
        "in_review": "under_review",
        "underreview": "under_review",
        "review": "under_review",
        "awaiting_approval": "under_review",
        "pending_approval": "under_review",
        # Twilio Content API uses these while Meta is reviewing
        "pending": "under_review",
        "received": "under_review",
        "submitted": "under_review",
        "not_submitted": "unsubmitted",
        "un_submitted": "unsubmitted",
    }
    s = aliases.get(s, s)
    if s == "approved":
        return "approved"
    if s in STATUS_TO_ERROR_CODE:
        return s
    if "reject" in s:
        return "rejected"
    if "pause" in s or "disable" in s:
        return "paused"
    if "review" in s or "pend" in s:
        return "under_review"
    if "submit" in s or "receive" in s:
        return "under_review"
    return "unknown"


def error_code_for_whatsapp_status(status: Optional[str]) -> str:
    return STATUS_TO_ERROR_CODE.get(normalize_whatsapp_approval_status(status), TEMPLATE_NOT_APPROVED)


def is_whatsapp_template_sendable(status: Optional[str]) -> bool:
    return normalize_whatsapp_approval_status(status) == "approved"


def mask_content_sid(content_sid: Optional[str], *, keep: int = 4) -> str:
    sid = (content_sid or "").strip()
    if not sid:
        return ""
    if len(sid) <= keep * 2:
        return sid[:2] + "…" + sid[-2:] if len(sid) > 4 else "***"
    return f"{sid[:keep]}…{sid[-keep:]}"


def display_status_label(status: Optional[str]) -> str:
    key = normalize_whatsapp_approval_status(status)
    return STATUS_DISPLAY.get(key, (status or "Unknown").replace("_", " ").title())


def status_emoji(status: Optional[str]) -> str:
    key = normalize_whatsapp_approval_status(status)
    return {
        "approved": "🟢",
        "under_review": "🟡",
        "pending": "🟡",
        "rejected": "🔴",
        "paused": "🟠",
        "unknown": "⚪",
    }.get(key, "⚪")


def extract_twilio_rest_error(exc: BaseException) -> tuple[Optional[str], Optional[str]]:
    code = getattr(exc, "code", None)
    msg = getattr(exc, "msg", None) or getattr(exc, "user_message", None) or str(exc)
    return (str(code) if code is not None else None), (str(msg)[:500] if msg else None)


_TEMPLATE_REASON_RE = re.compile(
    r"^template_(under_review|pending|rejected|paused|not_approved)$",
    re.I,
)


def is_template_approval_error_code(code: Optional[str]) -> bool:
    return bool(code and _TEMPLATE_REASON_RE.match(str(code).strip()))


def enrich_template_doc_from_info(doc: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
    """Merge Twilio Content + WhatsApp approval fields onto a local template document."""
    wa = normalize_whatsapp_approval_status(info.get("whatsapp_status"))
    out = dict(doc)
    out["whatsapp_approval_status"] = wa
    out["whatsapp_approval_label"] = display_status_label(wa)
    out["whatsapp_approval_emoji"] = status_emoji(wa)
    out["whatsapp_category"] = info.get("whatsapp_category") or info.get("category")
    out["whatsapp_language"] = info.get("language") or doc.get("language")
    out["whatsapp_eligibility"] = "approved" if wa == "approved" else "not_approved"
    bi = info.get("business_initiated")
    out["business_initiated"] = bool(bi) if bi is not None else (wa == "approved")
    out["user_initiated"] = info.get("user_initiated")
    out["content_sid_masked"] = mask_content_sid(info.get("content_sid") or doc.get("content_sid"))
    out["friendly_name"] = info.get("friendly_name") or doc.get("name")
    out["provider"] = info.get("provider") or "twilio_content"
    out["whatsapp_sendable"] = wa == "approved"
    return out
