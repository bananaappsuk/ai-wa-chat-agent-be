from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.mongo import get_db
from app.middleware.auth import current_user, require_tenant_resource
from app.services.activity import list_activity

router = APIRouter(tags=["activity"])


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date") from exc


@router.get("/activity")
async def tenant_activity(
    user: dict = Depends(current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    event_type: Optional[str] = Query(default=None, max_length=80),
    actor_id: Optional[str] = Query(default=None, max_length=40),
    created_from: Optional[str] = Query(default=None),
    created_to: Optional[str] = Query(default=None),
) -> dict:
    # Always self-tenant scoped (user_id == tenant_id in this product).
    tenant_id = str(user["_id"])
    return await list_activity(
        get_db(),
        tenant_id=tenant_id,
        page=page,
        page_size=page_size,
        event_type=event_type,
        actor_id=actor_id,
        created_from=_parse_dt(created_from),
        created_to=_parse_dt(created_to),
    )


@router.get("/leads/{lead_id}/activity")
async def lead_activity(
    lead_id: str,
    user: dict = Depends(current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    event_type: Optional[str] = Query(default=None, max_length=80),
) -> dict:
    await require_tenant_resource(
        collection="leads", resource_id=lead_id, user=user
    )
    return await list_activity(
        get_db(),
        tenant_id=str(user["_id"]),
        page=page,
        page_size=page_size,
        event_type=event_type,
        resource_type="lead",
        resource_id=lead_id,
    )


@router.get("/campaigns/{campaign_id}/activity")
async def campaign_activity(
    campaign_id: str,
    user: dict = Depends(current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    event_type: Optional[str] = Query(default=None, max_length=80),
) -> dict:
    await require_tenant_resource(
        collection="campaigns", resource_id=campaign_id, user=user
    )
    return await list_activity(
        get_db(),
        tenant_id=str(user["_id"]),
        page=page,
        page_size=page_size,
        event_type=event_type,
        resource_type="campaign",
        resource_id=campaign_id,
    )
