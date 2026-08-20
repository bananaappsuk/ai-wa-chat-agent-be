"""Apply Twilio delivery status callbacks to messages and blast recipients."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from bson import ObjectId

from app.db.mongo import get_db
from app.models.common import serialize, utcnow
from app.services.delivery_status import (
    build_status_update,
    is_failure_status,
    log_duplicate_sid,
    normalize_status,
)
from app.workers.queue import get_redis

logger = logging.getLogger(__name__)


def _publish_best_effort(user_id: str, event: str, data: dict) -> None:
    try:
        payload = json.dumps({"event": event, "data": data, "user_id": user_id})
        get_redis().publish("ws:events", payload)
    except Exception:
        logger.exception("Failed to publish %s for user_id=%s", event, user_id)


def _meta_error_fields(errors: Optional[list] = None) -> tuple[Optional[str], Optional[str]]:
    if not errors:
        return None, None
    first = next((e for e in errors if isinstance(e, dict)), None)
    if not first:
        return None, None
    code = first.get("code")
    text = first.get("title") or first.get("message")
    error_code = str(code).strip() if code is not None and str(code).strip() else None
    error_message = str(text).strip()[:500] if text is not None and str(text).strip() else None
    return error_code, error_message


def _pnid(value: Any) -> str:
    return str(value or "").strip()


async def apply_meta_status_update(
    *,
    provider_message_id: str,
    status_raw: str,
    errors: Optional[list] = None,
    phone_number_id: Optional[str] = None,
    db=None,
) -> dict[str, Any]:
    """Apply a Meta Cloud API delivery status to one outbound Meta message
    and matching campaign/blast recipients (by provider_message_id).
    """
    wamid = (provider_message_id or "").strip()
    if not wamid:
        return {"updated": False, "reason": "empty_id"}

    db = db if db is not None else get_db()
    doc = await db.messages.find_one(
        {
            "provider": "meta",
            "provider_message_id": wamid,
            "direction": "outbound",
        }
    )
    camp_recipients = await db.campaign_recipients.find(
        {"provider_message_id": wamid, "provider": "meta"}
    ).to_list(length=20)
    blast_recipients = await db.blast_recipients.find(
        {"provider_message_id": wamid}
    ).to_list(length=20)
    if not doc and not camp_recipients and not blast_recipients:
        logger.warning(
            "Meta status for unknown wamid suffix=...%s",
            wamid[-8:] if len(wamid) > 8 else "?",
        )
        return {"updated": False, "reason": "unknown"}

    user_id = str(
        (doc or {}).get("user_id")
        or (camp_recipients[0].get("user_id") if camp_recipients else "")
        or (blast_recipients[0].get("user_id") if blast_recipients else "")
        or ""
    )
    webhook_pnid = _pnid(phone_number_id)
    user = None
    if user_id and ObjectId.is_valid(user_id):
        user = await db.users.find_one({"_id": ObjectId(user_id)})
    user_pnids = {
        _pnid((user or {}).get("meta_phone_number_id")),
        _pnid((user or {}).get("meta_last_phone_number_id")),
    }
    user_pnids.discard("")

    if webhook_pnid and user_pnids and webhook_pnid not in user_pnids:
        logger.warning(
            "Meta status PNID mismatch user_id=%s wamid_suffix=...%s",
            user_id,
            wamid[-8:] if len(wamid) > 8 else "?",
        )
        return {"updated": False, "reason": "pnid_mismatch"}

    incoming = normalize_status(status_raw)
    if incoming is None:
        logger.info(
            "Meta status ignored unknown value wamid_suffix=...%s",
            wamid[-8:] if len(wamid) > 8 else "?",
        )
        return {"updated": False, "reason": "unknown_status"}

    error_code, error_message = _meta_error_fields(errors)
    applied = False
    last_doc = None
    if doc:
        set_fields = build_status_update(
            doc,
            incoming,
            error_code=error_code,
            error_message=error_message,
        )
        if set_fields:
            filt = {"_id": doc["_id"], "user_id": doc.get("user_id"), "provider": "meta"}
            await db.messages.update_one(filt, {"$set": set_fields})
            last_doc = await db.messages.find_one(filt) or {**doc, **set_fields}
            if user_id:
                _publish_best_effort(user_id, "message:updated", serialize(last_doc))
            applied = True

    for recipient in camp_recipients:
        result = await _update_campaign_recipient(
            db,
            recipient,
            status_raw=status_raw,
            error_code=error_code,
            error_message=error_message,
        )
        applied = applied or result.get("updated", False)

    for recipient in blast_recipients:
        result = await _update_blast_recipient(
            db,
            recipient,
            status_raw=status_raw,
            error_code=error_code,
            error_message=error_message,
        )
        applied = applied or result.get("updated", False)

    if not applied:
        return {"updated": False, "reason": "noop"}
    return {"updated": True, "reason": "applied", "kind": "message" if doc else "recipient", "doc": last_doc}


async def apply_twilio_status_callback(
    *,
    twilio_sid: str,
    status_raw: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
) -> dict[str, Any]:
    """
    Update message or blast recipient by Twilio SID.

    Returns a small result dict for tests/logging:
    ``{"updated": bool, "kind": "message"|"blast_recipient"|None, ...}``
    """
    db = get_db()
    messages = await db.messages.find({"twilio_sid": twilio_sid}).to_list(length=20)
    if messages:
        log_duplicate_sid("messages", twilio_sid, len(messages))
        updated_any = False
        last_doc = None
        for doc in messages:
            set_fields = build_status_update(
                doc,
                status_raw,
                error_code=error_code,
                error_message=error_message,
            )
            if not set_fields:
                continue
            await db.messages.update_one({"_id": doc["_id"]}, {"$set": set_fields})
            fresh = {**doc, **set_fields}
            last_doc = fresh
            updated_any = True
            user_id = str(doc.get("user_id") or "")
            if user_id:
                _publish_best_effort(user_id, "message:updated", serialize(fresh))
                new_st = str(set_fields.get("status") or "").lower()
                if new_st in ("failed", "undelivered"):
                    try:
                        from datetime import timedelta
                        from app.models.common import utcnow
                        from app.services.notifications import create_notification

                        since = utcnow() - timedelta(hours=1)
                        fail_n = await db.messages.count_documents(
                            {
                                "user_id": user_id,
                                "status": {"$in": ["failed", "undelivered"]},
                                "updated_at": {"$gte": since},
                            }
                        )
                        if fail_n >= 3:
                            await create_notification(
                                db,
                                user_id=user_id,
                                type="message_failed",
                                title="Repeated delivery failures",
                                message="Multiple WhatsApp messages failed to deliver recently.",
                                resource_type="message",
                                resource_id=str(doc["_id"]),
                                dedupe_key=f"msgfail:{user_id}:{int(since.timestamp()) // 3600}",
                            )
                    except Exception:
                        logger.exception("message failure notification failed")
        # Campaign engine stores the same Twilio SID on campaign_recipients
        camp_recipients = await db.campaign_recipients.find({"twilio_sid": twilio_sid}).to_list(length=20)
        for recipient in camp_recipients:
            await _update_campaign_recipient(
                db,
                recipient,
                status_raw=status_raw,
                error_code=error_code,
                error_message=error_message,
            )
        return {
            "updated": updated_any,
            "kind": "message",
            "count": len(messages),
            "doc": last_doc,
        }

    recipients = await db.blast_recipients.find({"twilio_sid": twilio_sid}).to_list(length=20)
    if recipients:
        log_duplicate_sid("blast_recipients", twilio_sid, len(recipients))
        updated_any = False
        for recipient in recipients:
            result = await _update_blast_recipient(
                db,
                recipient,
                status_raw=status_raw,
                error_code=error_code,
                error_message=error_message,
            )
            updated_any = updated_any or result.get("updated", False)
        return {"updated": updated_any, "kind": "blast_recipient", "count": len(recipients)}

    camp_recipients = await db.campaign_recipients.find({"twilio_sid": twilio_sid}).to_list(length=20)
    if not camp_recipients:
        logger.warning(
            "Twilio status callback for unknown sid suffix=...%s",
            twilio_sid[-6:] if len(twilio_sid) > 6 else "?",
        )
        return {"updated": False, "kind": None}

    log_duplicate_sid("campaign_recipients", twilio_sid, len(camp_recipients))
    updated_any = False
    for recipient in camp_recipients:
        result = await _update_campaign_recipient(
            db,
            recipient,
            status_raw=status_raw,
            error_code=error_code,
            error_message=error_message,
        )
        updated_any = updated_any or result.get("updated", False)
    return {"updated": updated_any, "kind": "campaign_recipient", "count": len(camp_recipients)}


async def _update_blast_recipient(
    db,
    recipient: dict,
    *,
    status_raw: str,
    error_code: Optional[str],
    error_message: Optional[str],
) -> dict[str, Any]:
    blast_id = recipient.get("blast_id")
    if not blast_id or not ObjectId.is_valid(str(blast_id)):
        logger.warning("Blast recipient missing valid blast_id")
        return {"updated": False}

    blast = await db.blast_campaigns.find_one({"_id": ObjectId(str(blast_id))})
    if not blast:
        logger.warning("Parent blast not found for recipient")
        return {"updated": False}

    user_id = str(blast.get("user_id") or "")
    old_status = normalize_status(recipient.get("status")) or (recipient.get("status") or "")
    set_fields = build_status_update(
        recipient,
        status_raw,
        error_code=error_code,
        error_message=error_message,
    )
    if not set_fields:
        return {"updated": False}

    await db.blast_recipients.update_one({"_id": recipient["_id"]}, {"$set": set_fields})
    new_status = set_fields.get("status") or old_status

    metric_inc: dict[str, int] = {}
    old_fail = is_failure_status(old_status)
    new_fail = is_failure_status(new_status)
    if new_fail and not old_fail:
        metric_inc["failed_count"] = 1

    # Legacy blasts stored Twilio "queued" as recipient status; that was never
    # counted in sent_count. When delivery webhooks arrive, promote the counter.
    counted_as_sent = ("sent", "delivered", "read", "queued", "accepted", "sending")
    if new_status in ("sent", "delivered", "read") and old_status not in counted_as_sent:
        metric_inc["sent_count"] = 1
    elif (
        new_status in ("delivered", "read")
        and old_status in ("queued", "accepted", "sending")
        and int(blast.get("sent_count") or 0) < int(blast.get("total_recipients") or 0)
    ):
        # Old bug: queued recipients left sent_count at 0; fix on delivery callback.
        metric_inc["sent_count"] = 1

    old_delivered = old_status in ("delivered", "read")
    new_delivered = new_status in ("delivered", "read")
    if new_delivered and not old_delivered:
        metric_inc["delivered_count"] = 1

    if new_status == "read" and old_status != "read":
        metric_inc["read_count"] = 1

    if new_status == "undelivered" and old_status != "undelivered":
        metric_inc["undelivered_count"] = 1

    if metric_inc:
        await db.blast_campaigns.update_one(
            {"_id": blast["_id"]},
            {"$inc": metric_inc, "$set": {"updated_at": utcnow()}},
        )

    fresh_blast = await db.blast_campaigns.find_one({"_id": blast["_id"]})
    if user_id and fresh_blast:
        _publish_best_effort(
            user_id,
            "blast:updated",
            {
                "id": str(blast["_id"]),
                "recipient_id": str(recipient["_id"]),
                "recipient_status": new_status,
                "sent_count": fresh_blast.get("sent_count", 0),
                "failed_count": fresh_blast.get("failed_count", 0),
                "delivered_count": fresh_blast.get("delivered_count", 0),
                "read_count": fresh_blast.get("read_count", 0),
                "undelivered_count": fresh_blast.get("undelivered_count", 0),
                "status": fresh_blast.get("status"),
                "total_recipients": fresh_blast.get("total_recipients"),
            },
        )
    return {"updated": True, "status": new_status}


async def _update_campaign_recipient(
    db,
    recipient: dict,
    *,
    status_raw: str,
    error_code: Optional[str],
    error_message: Optional[str],
) -> dict[str, Any]:
    from app.services.campaign_service import recipient_should_apply, recount_campaign_fields

    campaign_id = recipient.get("campaign_id")
    if not campaign_id:
        return {"updated": False}

    incoming = normalize_status(status_raw)
    if not incoming:
        return {"updated": False}

    # Map provider statuses into campaign recipient vocabulary
    mapped = incoming
    if incoming in ("accepted", "sending", "queued"):
        mapped = "sent" if recipient.get("status") in ("sent", "delivered", "read", "replied") else "sent"
    if incoming == "undelivered":
        mapped = "failed"
    if incoming == "canceled":
        mapped = "cancelled"

    old_status = recipient.get("status") or ""
    if not recipient_should_apply(old_status, mapped):
        return {"updated": False}

    set_fields: dict[str, Any] = {
        "status": mapped,
        "updated_at": utcnow(),
        "provider_status": incoming,
    }
    if error_code is not None:
        set_fields["error_code"] = str(error_code)
    if error_message is not None:
        set_fields["error_message"] = str(error_message)[:500]
    if mapped == "sent" and not recipient.get("sent_at"):
        set_fields["sent_at"] = utcnow()
    if mapped == "delivered":
        set_fields["delivered_at"] = utcnow()
    if mapped == "read":
        set_fields["read_at"] = utcnow()
    if mapped == "failed":
        set_fields["failed_at"] = utcnow()

    await db.campaign_recipients.update_one({"_id": recipient["_id"]}, {"$set": set_fields})

    user_id = str(recipient.get("user_id") or "")
    pipe = await db.campaign_recipients.aggregate(
        [{"$match": {"campaign_id": campaign_id}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    ).to_list(50)
    counts = {str(r["_id"]): int(r["n"]) for r in pipe}
    fields = recount_campaign_fields(counts, sum(counts.values()))
    fields["updated_at"] = utcnow()
    if ObjectId.is_valid(str(campaign_id)):
        await db.campaigns.update_one({"_id": ObjectId(str(campaign_id))}, {"$set": fields})
        fresh = await db.campaigns.find_one({"_id": ObjectId(str(campaign_id))})
        if user_id and fresh:
            _publish_best_effort(user_id, "campaign:updated", serialize(fresh))
            _publish_best_effort(
                user_id,
                "campaign:recipient_updated",
                serialize({**recipient, **set_fields}),
            )
    return {"updated": True, "status": mapped}
