"""Tenant-safe activity history (Mongo-backed)."""
from __future__ import annotations

import math
from typing import Any, Optional

from app.models.common import serialize, utcnow
from app.security.audit import audit as log_audit


_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "token",
        "reset_token",
        "auth_token",
        "twilio_auth_token",
        "secret",
        "api_key",
        "body",
        "message",
        "message_body",
    }
)


def _safe_metadata(meta: Optional[dict]) -> dict:
    out: dict[str, Any] = {}
    for k, v in (meta or {}).items():
        key = str(k).lower()
        if key in _SENSITIVE_KEYS or key.endswith("_token") or "password" in key:
            continue
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[str(k)[:40]] = v if not isinstance(v, str) else v[:200]
    return out


async def record_activity(
    db,
    *,
    tenant_id: str,
    event_type: str,
    summary: str,
    actor_id: Optional[str] = None,
    actor_name: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    doc = {
        "tenant_id": tenant_id,
        "user_id": tenant_id,  # single-tenant-per-user model
        "event_type": (event_type or "event")[:80],
        "summary": (summary or "")[:300],
        "actor_id": actor_id,
        "actor_name": (actor_name or "")[:100] or None,
        "resource_type": (resource_type or "")[:40] or None,
        "resource_id": str(resource_id) if resource_id else None,
        "metadata": _safe_metadata(metadata),
        "created_at": utcnow(),
    }
    res = await db.activity_events.insert_one(doc)
    doc["_id"] = res.inserted_id
    # Also emit structured audit log (no secrets)
    log_audit(
        event_type,
        user_id=actor_id or tenant_id,
        target_id=str(resource_id) if resource_id else None,
        extra={"summary": summary[:120]},
    )
    return doc


def record_activity_sync(
    db,
    *,
    tenant_id: str,
    event_type: str,
    summary: str,
    actor_id: Optional[str] = None,
    actor_name: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """Sync variant for RQ workers (pymongo). Mirrors record_activity's fields."""
    doc = {
        "tenant_id": tenant_id,
        "user_id": tenant_id,
        "event_type": (event_type or "event")[:80],
        "summary": (summary or "")[:300],
        "actor_id": actor_id,
        "actor_name": (actor_name or "")[:100] or None,
        "resource_type": (resource_type or "")[:40] or None,
        "resource_id": str(resource_id) if resource_id else None,
        "metadata": _safe_metadata(metadata),
        "created_at": utcnow(),
    }
    res = db.activity_events.insert_one(doc)
    doc["_id"] = res.inserted_id
    log_audit(
        event_type,
        user_id=actor_id or tenant_id,
        target_id=str(resource_id) if resource_id else None,
        extra={"summary": (summary or "")[:120]},
    )
    return doc


async def list_activity(
    db,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = 25,
    event_type: Optional[str] = None,
    actor_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    created_from=None,
    created_to=None,
) -> dict:
    page = max(1, int(page))
    page_size = min(100, max(1, int(page_size)))
    filt: dict[str, Any] = {"tenant_id": tenant_id}
    if event_type:
        filt["event_type"] = event_type.strip()[:80]
    if actor_id:
        filt["actor_id"] = actor_id
    if resource_type:
        filt["resource_type"] = resource_type.strip()[:40]
    if resource_id:
        filt["resource_id"] = str(resource_id)
    if created_from or created_to:
        rng = {}
        if created_from:
            rng["$gte"] = created_from
        if created_to:
            rng["$lte"] = created_to
        filt["created_at"] = rng

    total = await db.activity_events.count_documents(filt)
    total_pages = math.ceil(total / page_size) if total else 0
    skip = (page - 1) * page_size
    items = [
        serialize(d)
        async for d in db.activity_events.find(filt)
        .sort("created_at", -1)
        .skip(skip)
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
