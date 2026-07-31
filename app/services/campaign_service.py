"""Campaign sending engine helpers (E1/E2)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from bson import ObjectId

from app.config import settings
from app.models.campaign import EDITABLE_STATUSES, TERMINAL_STATUSES, empty_campaign_counters
from app.models.common import utcnow
from app.services.whatsapp_window import WINDOW_CLOSED_ERROR, is_whatsapp_window_open

RECIPIENT_TERMINAL = frozenset(
    {"sent", "delivered", "read", "failed", "skipped", "cancelled", "replied"}
)
# Statuses still waiting to send
RECIPIENT_OPEN = frozenset({"pending", "queued", "processing", "retrying"})

# Monotonic ranks for campaign recipient delivery progression
_RECIPIENT_RANK = {
    "pending": 0,
    "queued": 10,
    "processing": 20,
    "retrying": 25,
    "sent": 40,
    "delivered": 50,
    "read": 60,
    "replied": 70,
    "failed": 100,
    "skipped": 100,
    "cancelled": 100,
}


def parse_scheduled_at(raw: Optional[str]) -> Optional[datetime]:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_phone(raw: str) -> Optional[str]:
    from app.services.phone_norm import normalize_e164

    return normalize_e164(raw)


def progress_percentage(campaign: dict) -> float:
    total = int(campaign.get("total_recipients") or 0)
    if total <= 0:
        return 0.0
    done = (
        int(campaign.get("sent_count") or 0)
        + int(campaign.get("delivered_count") or 0)
        + int(campaign.get("read_count") or 0)
        + int(campaign.get("failed_count") or 0)
        + int(campaign.get("skipped_count") or 0)
        + int(campaign.get("cancelled_count") or 0)
    )
    # Prefer terminal-ish accounting without double-counting delivered under sent.
    # Use: total - still open
    open_n = (
        int(campaign.get("queued_count") or 0)
        + int(campaign.get("processing_count") or 0)
    )
    # Fallback using completed terminal counts stored on campaign
    terminal = (
        int(campaign.get("failed_count") or 0)
        + int(campaign.get("skipped_count") or 0)
        + int(campaign.get("cancelled_count") or 0)
        + int(campaign.get("sent_count") or 0)
    )
    pct = min(100.0, round(100.0 * terminal / total, 1))
    return pct


def compute_rates(campaign: dict, status_counts: Optional[dict[str, int]] = None) -> dict[str, float]:
    sent_ok = int(campaign.get("sent_count") or 0)
    delivered = int(campaign.get("delivered_count") or 0)
    read = int(campaign.get("read_count") or 0)
    failed = int(campaign.get("failed_count") or 0)
    replied = int(campaign.get("replied_count") or 0)
    total = int(campaign.get("total_recipients") or 0)
    skipped = int(campaign.get("skipped_count") or 0)
    cancelled = int(campaign.get("cancelled_count") or 0)

    delivered_or_read = delivered + read
    # Successfully handed to Twilio (sent+delivered+read) — sent_count includes provider accept
    successfully_sent = max(sent_ok, delivered_or_read)
    attempted = successfully_sent + failed
    terminal = successfully_sent + failed + skipped + cancelled

    def pct(num: float, den: float) -> float:
        if den <= 0:
            return 0.0
        return round(100.0 * num / den, 1)

    return {
        "delivery_rate": pct(delivered_or_read, successfully_sent),
        "read_rate": pct(read, delivered_or_read),
        "failure_rate": pct(failed, attempted),
        "reply_rate": pct(replied, successfully_sent),
        "completion_rate": pct(terminal, total),
    }


def recipient_should_apply(current: Optional[str], incoming: str) -> bool:
    if not incoming:
        return False
    cur = current or ""
    if cur == incoming:
        return False
    # Never move backwards on success path
    if cur in ("replied",) and incoming in ("sent", "delivered", "read"):
        return False
    if cur == "read" and incoming in ("sent", "delivered", "queued", "processing"):
        return False
    if cur == "delivered" and incoming in ("sent", "queued", "processing"):
        return False
    if cur in ("failed", "skipped", "cancelled") and incoming not in ("retrying", "queued", "pending"):
        # Allow retry transition only via explicit retry flow
        if incoming in ("sent", "delivered", "read"):
            return False
    cur_r = _RECIPIENT_RANK.get(cur, 0)
    new_r = _RECIPIENT_RANK.get(incoming, 0)
    if incoming == "failed" and cur in ("sent", "delivered", "read", "replied"):
        return True  # provider failure after send
    if incoming == "replied":
        return cur not in ("cancelled", "skipped")
    return new_r >= cur_r or incoming in ("failed", "skipped", "cancelled")


def is_retryable_error(error: str) -> bool:
    from app.services.twilio_errors import classify_send_error, is_retryable_category

    if not (error or "").strip():
        return False
    return is_retryable_category(classify_send_error(error))


def finalize_status_from_counts(campaign: dict) -> str:
    total = int(campaign.get("total_recipients") or 0)
    failed = int(campaign.get("failed_count") or 0)
    skipped = int(campaign.get("skipped_count") or 0)
    cancelled = int(campaign.get("cancelled_count") or 0)
    sent = int(campaign.get("sent_count") or 0)
    if campaign.get("status") == "cancelled":
        return "cancelled"
    if total == 0:
        return "failed"
    if cancelled and sent == 0 and failed == 0:
        return "cancelled"
    # Nothing actually sent — treat as failed (e.g. all skipped for consent).
    if sent == 0 and (failed + skipped + cancelled) >= total:
        return "failed"
    if sent > 0 and (failed > 0 or skipped > 0 or cancelled > 0):
        return "partially_completed"
    if sent > 0:
        return "completed"
    return "completed"


def empty_recipient_ai_fields() -> dict:
    return {
        "content_source": None,
        "generated_message": None,
        "generated_template_variables": None,
        "ai_generation_status": "pending",
        "ai_generation_error_category": None,
        "ai_model": None,
        "ai_input_tokens": 0,
        "ai_output_tokens": 0,
        "ai_estimated_cost": 0.0,
        "ai_generated_at": None,
        "ai_approved": False,
        "ai_approved_at": None,
        "ai_finish_reason": None,
        "ai_idempotency_key": None,
        "generation_version": 0,
        "ai_warnings": [],
        "is_preview_only": False,
    }


async def build_recipient_rows(
    db,
    *,
    user_id: str,
    campaign_id: str,
    lead_ids: Optional[list[str]],
    phones: Optional[list[str]],
) -> tuple[list[dict], list[dict]]:
    """Return (rows_to_insert, skipped_meta)."""
    blacklist = {
        d["phone"]
        async for d in db.blacklist.find({"user_id": user_id}, {"phone": 1})
    }
    seen: set[str] = set()
    rows: list[dict] = []
    skipped: list[dict] = []
    now = utcnow()

    async def add_phone(phone_raw: str, lead: Optional[dict] = None) -> None:
        phone = normalize_phone(phone_raw)
        if not phone:
            skipped.append({"phone": phone_raw, "reason": "invalid phone"})
            return
        if phone in seen:
            skipped.append({"phone": phone, "reason": "duplicate"})
            return
        seen.add(phone)
        # Marketing/campaign eligibility — never auto opt-in imports
        from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility

        lead_doc = lead or {"phone": phone, "blacklisted": phone in blacklist}
        if phone in blacklist:
            lead_doc = {**lead_doc, "blacklisted": True}
        elig = get_whatsapp_send_eligibility(
            lead=lead_doc,
            phone=phone,
            purpose="campaign",
            has_template=False,  # window/template checked at send; consent/blacklist here
            blacklisted=phone in blacklist or bool(lead_doc.get("blacklisted")),
        )
        # At build time we only hard-skip consent/blacklist/invalid; window handled at send
        if not elig.allowed and elig.reason_code in (
            "consent_blocked",
            "consent_required",
            "invalid_recipient",
        ):
            rows.append(
                {
                    "campaign_id": campaign_id,
                    "user_id": user_id,
                    "lead_id": str(lead["_id"]) if lead else None,
                    "phone": phone,
                    "name": (lead or {}).get("name"),
                    "status": "skipped",
                    "error_message": elig.reason_code,
                    "message_purpose": "campaign",
                    "attempt_count": 0,
                    "created_at": now,
                    "updated_at": now,
                    **empty_recipient_ai_fields(),
                }
            )
            return
        if phone in blacklist:
            rows.append(
                {
                    "campaign_id": campaign_id,
                    "user_id": user_id,
                    "lead_id": str(lead["_id"]) if lead else None,
                    "phone": phone,
                    "name": (lead or {}).get("name"),
                    "status": "skipped",
                    "error_message": "blacklisted",
                    "message_purpose": "campaign",
                    "attempt_count": 0,
                    "created_at": now,
                    "updated_at": now,
                    **empty_recipient_ai_fields(),
                }
            )
            return
        rows.append(
            {
                "campaign_id": campaign_id,
                "user_id": user_id,
                "lead_id": str(lead["_id"]) if lead and lead.get("_id") else None,
                "phone": phone,
                "name": (lead or {}).get("name"),
                "status": "pending",
                "message_purpose": "campaign",
                "attempt_count": 0,
                "created_at": now,
                "updated_at": now,
                **empty_recipient_ai_fields(),
            }
        )

    if lead_ids:
        for lid in lead_ids:
            if not ObjectId.is_valid(lid):
                skipped.append({"lead_id": lid, "reason": "invalid lead id"})
                continue
            lead = await db.leads.find_one({"_id": ObjectId(lid), "user_id": user_id})
            if not lead:
                skipped.append({"lead_id": lid, "reason": "lead not found"})
                continue
            if not lead.get("phone"):
                skipped.append({"lead_id": lid, "reason": "lead has no phone"})
                continue
            await add_phone(lead["phone"], lead)

    if phones:
        for raw in phones:
            # Prefer matching lead for name/window later
            norm = normalize_phone(raw)
            lead = None
            if norm:
                lead = await db.leads.find_one({"user_id": user_id, "phone": norm})
            await add_phone(raw, lead)

    return rows, skipped


def recount_campaign_fields(status_counts: dict[str, int], total: int) -> dict[str, Any]:
    sent = int(status_counts.get("sent", 0))
    delivered = int(status_counts.get("delivered", 0))
    read = int(status_counts.get("read", 0))
    replied = int(status_counts.get("replied", 0))
    # Treat delivered/read/replied as successfully sent for sent_count display
    sent_total = sent + delivered + read + replied
    failed = int(status_counts.get("failed", 0))
    skipped = int(status_counts.get("skipped", 0))
    cancelled = int(status_counts.get("cancelled", 0))
    queued = int(status_counts.get("queued", 0)) + int(status_counts.get("pending", 0)) + int(
        status_counts.get("retrying", 0)
    )
    processing = int(status_counts.get("processing", 0))
    fields = {
        "total_recipients": total,
        "queued_count": queued,
        "processing_count": processing,
        "sent_count": sent_total,
        "delivered_count": delivered + read + replied,  # delivered-or-better
        "read_count": read + replied,
        "failed_count": failed,
        "skipped_count": skipped,
        "cancelled_count": cancelled,
        "replied_count": replied,
    }
    fields["progress_percentage"] = progress_percentage(fields)
    return fields


def recount_ai_generation_fields(ai_status_counts: dict[str, int]) -> dict[str, int]:
    """AI generation counters — intentional policy skips are not generation failures."""
    return {
        "ai_ready_count": int(ai_status_counts.get("ready", 0)),
        "ai_review_count": int(ai_status_counts.get("needs_review", 0)),
        "ai_failed_count": int(ai_status_counts.get("failed", 0)),
    }
