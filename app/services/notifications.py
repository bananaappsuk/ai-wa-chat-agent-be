"""In-app notifications (tenant/user scoped)."""
from __future__ import annotations

import math
from typing import Any, Optional

from bson import ObjectId

from app.models.common import serialize, utcnow
from app.services.ws_manager import ws_manager

ALLOWED_TYPES = frozenset(
    {
        "needs_human",
        "takeover_requested",
        "campaign_completed",
        "campaign_failed",
        "message_failed",
        "consent_opt_out",
        "import_completed",
        "security_notice",
        "system",
        "campaign_ai_preview_ready",
        "campaign_ai_review_ready",
        "campaign_ai_generation_complete",
        "campaign_ai_generation_partial",
        "campaign_ai_quota",
    }
)


async def create_notification(
    db,
    *,
    user_id: str,
    type: str,
    title: str,
    message: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
) -> Optional[dict]:
    ntype = (type or "system").strip()[:40]
    if ntype not in ALLOWED_TYPES:
        ntype = "system"
    now = utcnow()
    if dedupe_key:
        existing = await db.notifications.find_one(
            {"user_id": user_id, "dedupe_key": dedupe_key[:120]}
        )
        if existing:
            return None

    doc = {
        "user_id": user_id,
        "tenant_id": user_id,
        "type": ntype,
        "title": (title or "")[:120],
        "message": (message or "")[:300],
        "resource_type": (resource_type or "")[:40] or None,
        "resource_id": str(resource_id) if resource_id else None,
        "is_read": False,
        "created_at": now,
        "read_at": None,
        "dedupe_key": (dedupe_key or "")[:120] or None,
    }
    res = await db.notifications.insert_one(doc)
    doc["_id"] = res.inserted_id
    try:
        await ws_manager.push(user_id, "notification:new", serialize(doc))
    except Exception:
        pass
    return doc


async def list_notifications(
    db,
    *,
    user_id: str,
    page: int = 1,
    page_size: int = 25,
    unread_only: bool = False,
) -> dict:
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    filt: dict[str, Any] = {"user_id": user_id}
    if unread_only:
        filt["is_read"] = False
    total = await db.notifications.count_documents(filt)
    total_pages = math.ceil(total / page_size) if total else 0
    items = [
        serialize(d)
        async for d in db.notifications.find(filt)
        .sort("created_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    ]
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1 and total > 0,
    }


async def unread_count(db, *, user_id: str) -> int:
    return int(await db.notifications.count_documents({"user_id": user_id, "is_read": False}))


async def mark_read(db, *, user_id: str, notification_id: str) -> bool:
    if not ObjectId.is_valid(notification_id):
        return False
    res = await db.notifications.update_one(
        {"_id": ObjectId(notification_id), "user_id": user_id, "is_read": False},
        {"$set": {"is_read": True, "read_at": utcnow()}},
    )
    return res.modified_count > 0


async def mark_all_read(db, *, user_id: str) -> int:
    res = await db.notifications.update_many(
        {"user_id": user_id, "is_read": False},
        {"$set": {"is_read": True, "read_at": utcnow()}},
    )
    return int(res.modified_count or 0)


async def delete_notification(db, *, user_id: str, notification_id: str) -> bool:
    if not ObjectId.is_valid(notification_id):
        return False
    res = await db.notifications.delete_one(
        {"_id": ObjectId(notification_id), "user_id": user_id}
    )
    return res.deleted_count > 0


def create_notification_sync(
    db,
    *,
    user_id: str,
    type: str,
    title: str,
    message: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    dedupe_key: Optional[str] = None,
    publish_ws: bool = True,
) -> Optional[dict]:
    """Sync variant for RQ workers (pymongo)."""
    import json
    from datetime import datetime, timezone

    ntype = (type or "system").strip()[:40]
    if ntype not in ALLOWED_TYPES:
        ntype = "system"
    now = datetime.now(timezone.utc)
    if dedupe_key:
        existing = db.notifications.find_one(
            {"user_id": user_id, "dedupe_key": dedupe_key[:120]}
        )
        if existing:
            return None
    doc = {
        "user_id": user_id,
        "tenant_id": user_id,
        "type": ntype,
        "title": (title or "")[:120],
        "message": (message or "")[:300],
        "resource_type": (resource_type or "")[:40] or None,
        "resource_id": str(resource_id) if resource_id else None,
        "is_read": False,
        "created_at": now,
        "read_at": None,
        "dedupe_key": (dedupe_key or "")[:120] or None,
    }
    res = db.notifications.insert_one(doc)
    doc["_id"] = res.inserted_id
    if publish_ws:
        try:
            from redis import Redis
            from app.config import settings

            payload = {
                "event": "notification:new",
                "data": {
                    "id": str(doc["_id"]),
                    "type": doc["type"],
                    "title": doc["title"],
                    "message": doc["message"],
                    "resource_type": doc.get("resource_type"),
                    "resource_id": doc.get("resource_id"),
                    "is_read": False,
                    "created_at": now.isoformat(),
                },
                "user_id": user_id,
            }
            Redis.from_url(settings.REDIS_URL).publish("ws:events", json.dumps(payload))
        except Exception:
            pass
    return doc
