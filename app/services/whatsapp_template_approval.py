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


def library_status_for(wa_status: Optional[str], current: Optional[str]) -> dict:
    """Templates created in the app follow WhatsApp's decision in the library."""
    if wa_status == "approved" and current != "approved":
        return {"status": "approved"}
    if wa_status == "rejected" and current != "rejected":
        return {"status": "rejected"}
    return {}


def refresh_app_created_templates(limit: int = 50) -> int:
    """Poll WhatsApp approval for templates created in the app and still under review; notify
    the user when one is approved or rejected."""
    from datetime import datetime, timedelta, timezone

    from app.services import twilio_service
    from app.services.notifications import create_notification_sync
    from app.workers.tasks import _db

    db = _db()
    now = datetime.now(timezone.utc)
    pending = db.templates.find(
        {
            "created_via": "app",
            "content_sid": {"$nin": [None, ""]},
            "whatsapp_approval_status": {"$nin": ["approved", "rejected", "paused", "disabled"]},
            "created_at": {"$gte": now - timedelta(days=14)},
        }
    ).limit(limit)
    changed = 0
    for t in pending:
        try:
            info = twilio_service.get_content_template_info(t["content_sid"])
        except Exception:
            continue
        wa = info.get("whatsapp_status")
        update = {"whatsapp_approval_status": wa, "whatsapp_approval_checked_at": now,
                  "whatsapp_rejection_reason": info.get("rejection_reason"), **library_status_for(wa, t.get("status"))}
        db.templates.update_one({"_id": t["_id"]}, {"$set": update})
        if wa in ("approved", "rejected") and wa != t.get("whatsapp_approval_status"):
            changed += 1
            reason = info.get("rejection_reason")
            create_notification_sync(
                db,
                user_id=t["user_id"],
                type="system",
                title=f"Template {'approved' if wa == 'approved' else 'rejected'}: {t.get('name')}",
                message=("WhatsApp approved it — you can send it now." if wa == "approved"
                         else f"WhatsApp rejected it{': ' + reason if reason else ''}. Edit the text and create it again."),
                resource_type="template",
                resource_id=str(t["_id"]),
                dedupe_key=f"template_status:{t['_id']}:{wa}",
            )
    return changed


_LIVE_MESSAGES = {
    "paused": ("Template paused by WhatsApp: {name}",
               "WhatsApp paused it because people reported or blocked it. Sends with it fail until it's approved again "
               "(usually a few hours)."),
    "disabled": ("Template disabled by WhatsApp: {name}", "It can't be sent any more. Create a new template to keep sending."),
    "rejected": ("Template rejected by WhatsApp: {name}", "It can't be sent. Duplicate it, fix the text and submit again."),
    "approved": ("Template approved again: {name}", "WhatsApp lifted the pause — you can send it again."),
}


def _pause_sends_using(db, user_id: str, template_id: str, content_sid: str, status: str) -> int:
    from datetime import datetime, timezone

    reason = f"WhatsApp {status} the template — press Resume once it's approved again."
    match = {"user_id": user_id, "$or": [{"template_id": template_id}, {"content_sid": content_sid}]}
    now = datetime.now(timezone.utc)
    b = db.blast_campaigns.update_many({**match, "status": {"$in": ["queued", "sending"]}},
                                       {"$set": {"status": "paused", "pause_reason": reason, "updated_at": now}})
    c = db.campaigns.update_many({**match, "status": {"$in": ["queued", "running"]}},
                                 {"$set": {"status": "paused", "pause_reason": reason, "paused_at": now, "updated_at": now}})
    return b.modified_count + c.modified_count


def refresh_live_templates(limit: int = 200) -> int:
    """Watch templates that are live (approved) or paused: when WhatsApp pauses, disables or
    re-approves one, update it and tell the user."""
    from datetime import datetime, timezone

    from app.services import twilio_service
    from app.services.notifications import create_notification_sync
    from app.workers.tasks import _db

    db = _db()
    now = datetime.now(timezone.utc)
    watched = db.templates.find(
        {"provider": {"$in": [None, "twilio_content"]}, "content_sid": {"$nin": [None, ""]},
         "whatsapp_approval_status": {"$in": ["approved", "paused"]}},
        {"user_id": 1, "name": 1, "content_sid": 1, "whatsapp_approval_status": 1, "status": 1},
    ).limit(limit)
    changed = 0
    for t in watched:
        try:
            info = twilio_service.get_content_template_info(t["content_sid"])
        except Exception:
            continue
        old, new = t.get("whatsapp_approval_status"), info.get("whatsapp_status")
        if not new or new == "unknown" or new == old:
            continue
        update = {"whatsapp_approval_status": new, "whatsapp_approval_checked_at": now,
                  "whatsapp_rejection_reason": info.get("rejection_reason")}
        if new in ("disabled", "rejected") and t.get("status") == "approved":
            update["status"] = "rejected"
        db.templates.update_one({"_id": t["_id"]}, {"$set": update})
        stopped = _pause_sends_using(db, t["user_id"], str(t["_id"]), t["content_sid"], new) if new in ("paused", "disabled", "rejected") else 0
        msg = _LIVE_MESSAGES.get(new) if not (new == "approved" and old != "paused") else None
        if msg:
            changed += 1
            extra = f" {stopped} running blast/campaign{'s' if stopped != 1 else ''} using it {'were' if stopped != 1 else 'was'} paused — press Resume once it's approved again." if stopped else ""
            create_notification_sync(
                db, user_id=t["user_id"], type="system", title=msg[0].format(name=t.get("name")), message=msg[1] + extra,
                resource_type="template", resource_id=str(t["_id"]), dedupe_key=f"template_live:{t['_id']}:{new}:{now:%Y%m%d%H}",
            )
    return changed
