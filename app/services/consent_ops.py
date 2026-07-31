"""Shared helpers for consent mutations, audit, and campaign cancel on opt-out."""
from __future__ import annotations

from typing import Any, Optional

from bson import ObjectId

from app.models.common import utcnow
from app.security.audit import audit
from app.services.whatsapp_consent import ConsentSource, ConsentStatus, consent_update_fields


async def apply_consent_change(
    db,
    *,
    user_id: str,
    lead_id: str,
    status: ConsentStatus,
    source: ConsentSource | str,
    proof: Optional[str] = None,
    reason: Optional[str] = None,
    changed_by: Optional[str] = None,
    phone: Optional[str] = None,
    request_id: Optional[str] = None,
) -> dict | None:
    """Update lead consent (+ blacklist sync). Returns updated lead or None."""
    if not ObjectId.is_valid(lead_id):
        return None
    fields = consent_update_fields(
        status=status,
        source=source,
        proof=proof,
        reason=reason,
        changed_by=changed_by,
    )
    res = await db.leads.update_one(
        {"_id": ObjectId(lead_id), "user_id": user_id},
        {"$set": fields},
    )
    if res.matched_count == 0:
        return None

    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    phone_val = phone or (lead or {}).get("phone")
    if phone_val and status == "opted_out":
        await db.blacklist.update_one(
            {"user_id": user_id, "phone": phone_val},
            {"$set": {"reason": reason or "opted_out", "created_at": utcnow(), "source": source}},
            upsert=True,
        )
        await cancel_pending_for_phone(db, user_id=user_id, phone=phone_val)
    elif phone_val and status == "opted_in":
        await db.blacklist.delete_one({"user_id": user_id, "phone": phone_val})

    event = "consent.opted_in" if status == "opted_in" else "consent.opted_out"
    if source in ("keyword_optout", "keyword_optin"):
        event = f"consent.{source}"
    audit(event, user_id=user_id, target_id=lead_id, request_id=request_id)

    # Consent history event (compliance)
    await db.consent_events.insert_one(
        {
            "user_id": user_id,
            "lead_id": lead_id,
            "phone": phone_val,
            "status": status,
            "source": source,
            "proof": (proof or "")[:500] or None,
            "reason": (reason or "")[:200] or None,
            "changed_by": changed_by,
            "created_at": utcnow(),
        }
    )
    return lead


async def cancel_pending_for_phone(db, *, user_id: str, phone: str) -> int:
    now = utcnow()
    res = await db.campaign_recipients.update_many(
        {
            "user_id": user_id,
            "phone": phone,
            "status": {"$in": ["pending", "queued", "retrying", "processing"]},
        },
        {
            "$set": {
                "status": "cancelled",
                "error_message": "opted_out",
                "updated_at": now,
            }
        },
    )
    return int(res.modified_count or 0)


def sync_apply_consent_change(
    db,
    *,
    user_id: str,
    lead_id: str,
    status: ConsentStatus,
    source: ConsentSource | str,
    proof: Optional[str] = None,
    reason: Optional[str] = None,
    changed_by: Optional[str] = None,
    phone: Optional[str] = None,
) -> Optional[dict]:
    """Sync Mongo variant for RQ workers / webhook thread helpers."""
    if not ObjectId.is_valid(lead_id):
        return None
    fields = consent_update_fields(
        status=status,
        source=source,
        proof=proof,
        reason=reason,
        changed_by=changed_by,
    )
    res = db.leads.update_one(
        {"_id": ObjectId(lead_id), "user_id": user_id},
        {"$set": fields},
    )
    if res.matched_count == 0:
        return None
    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    phone_val = phone or (lead or {}).get("phone")
    if phone_val and status == "opted_out":
        db.blacklist.update_one(
            {"user_id": user_id, "phone": phone_val},
            {"$set": {"reason": reason or "opted_out", "created_at": utcnow(), "source": source}},
            upsert=True,
        )
        db.campaign_recipients.update_many(
            {
                "user_id": user_id,
                "phone": phone_val,
                "status": {"$in": ["pending", "queued", "retrying", "processing"]},
            },
            {
                "$set": {
                    "status": "cancelled",
                    "error_message": "opted_out",
                    "updated_at": utcnow(),
                }
            },
        )
    elif phone_val and status == "opted_in":
        db.blacklist.delete_one({"user_id": user_id, "phone": phone_val})
    db.consent_events.insert_one(
        {
            "user_id": user_id,
            "lead_id": lead_id,
            "phone": phone_val,
            "status": status,
            "source": source,
            "proof": (proof or "")[:500] or None,
            "reason": (reason or "")[:200] or None,
            "changed_by": changed_by,
            "created_at": utcnow(),
        }
    )
    return lead


def consent_snapshot(lead: dict | None) -> dict[str, Any]:
    from app.services.whatsapp_consent import apply_consent_defaults

    lead = apply_consent_defaults(dict(lead or {}))
    return {
        "whatsapp_consent_status": lead.get("whatsapp_consent_status") or "unknown",
        "whatsapp_consent_source": lead.get("whatsapp_consent_source"),
        "whatsapp_consent_at": lead.get("whatsapp_consent_at"),
        "whatsapp_consent_updated_at": lead.get("whatsapp_consent_updated_at"),
        "whatsapp_consent_proof": lead.get("whatsapp_consent_proof"),
        "whatsapp_opted_out_at": lead.get("whatsapp_opted_out_at"),
        "whatsapp_opt_out_reason": lead.get("whatsapp_opt_out_reason"),
        "blacklisted": bool(lead.get("blacklisted")),
    }
