"""Reconcile stale outbound messages stuck in queued/sending."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId

from app.config import settings

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def reconcile_stale_messages(*, batch_size: int | None = None) -> dict[str, Any]:
    """
    Sync worker/scheduler entrypoint.
    Finds outbound messages stuck in queued/sending and marks them failed when stale.
    Optionally queries Twilio when a SID exists (best-effort, rate-limited by batch).
    """
    from pymongo import MongoClient
    from app.services import twilio_service

    batch = max(1, int(batch_size or settings.RECONCILIATION_BATCH_SIZE))
    stale_min = max(5, int(settings.MESSAGE_STALE_AFTER_MINUTES))
    cutoff = _utcnow() - timedelta(minutes=stale_min)

    client = MongoClient(settings.MONGO_URI)
    db = client[settings.MONGO_DB]
    repaired = 0
    inspected = 0
    twilio_lookups = 0

    cur = (
        db.messages.find(
            {
                "direction": "outbound",
                "status": {"$in": ["queued", "sending", "accepted"]},
                "created_at": {"$lt": cutoff},
            }
        )
        .sort("created_at", 1)
        .limit(batch)
    )

    for msg in cur:
        inspected += 1
        sid = (msg.get("twilio_sid") or "").strip()
        new_status = None
        err = None
        if sid and settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN:
            try:
                twilio_lookups += 1
                fetched = twilio_service.fetch_message_status(sid)
                if fetched:
                    new_status = fetched.get("status")
                    err = fetched.get("error_message")
            except Exception:
                logger.debug("Twilio status fetch failed for reconciliation", exc_info=True)

        if new_status in ("delivered", "read", "sent", "failed", "undelivered"):
            app_status = new_status
            if new_status in ("undelivered",):
                app_status = "failed"
            db.messages.update_one(
                {"_id": msg["_id"]},
                {
                    "$set": {
                        "status": app_status,
                        "provider_status": new_status,
                        "error": (err or msg.get("error") or "")[:500] or None,
                        "final_failure_reason": err if app_status == "failed" else None,
                        "reconciled_at": _utcnow(),
                    }
                },
            )
            repaired += 1
            continue

        # Permanently stale without provider confirmation
        db.messages.update_one(
            {"_id": msg["_id"], "status": {"$in": ["queued", "sending", "accepted"]}},
            {
                "$set": {
                    "status": "failed",
                    "error": "Message stale — marked failed by reconciliation",
                    "final_failure_reason": "stale_timeout",
                    "reconciled_at": _utcnow(),
                }
            },
        )
        repaired += 1

        # Repair campaign recipient if linked
        rid = msg.get("campaign_recipient_id")
        if rid and ObjectId.is_valid(str(rid)):
            db.campaign_recipients.update_one(
                {
                    "_id": ObjectId(str(rid)),
                    "status": {"$in": ["processing", "queued", "sent"]},
                },
                {
                    "$set": {
                        "status": "failed",
                        "error_message": "stale_timeout",
                        "updated_at": _utcnow(),
                    }
                },
            )

    try:
        from app.observability.metrics import inc_reconciliation_repairs

        inc_reconciliation_repairs(repaired)
    except Exception:
        pass

    return {
        "inspected": inspected,
        "repaired": repaired,
        "twilio_lookups": twilio_lookups,
        "stale_after_minutes": stale_min,
    }
