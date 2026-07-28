"""Tenant-safe AI analytics endpoints."""
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.services import ai_analytics
from app.services.ai_config import resolve_ai_settings

router = APIRouter(prefix="/analytics/ai", tags=["analytics"])


@router.get("/overview")
async def ai_overview(
    user: dict = Depends(current_user),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
) -> dict:
    # Self-tenant scoped — no elevated role required
    ai = resolve_ai_settings(user)
    if not ai.get("analytics_enabled"):
        return {"enabled": False, "message": "AI analytics disabled"}
    data = await ai_analytics.overview(
        get_db(), tenant_id=str(user["_id"]), date_from=date_from, date_to=date_to
    )
    data["enabled"] = True
    return data


@router.get("/usage")
async def ai_usage(
    user: dict = Depends(current_user),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
) -> dict:
    return await ai_analytics.usage_page(
        get_db(),
        tenant_id=str(user["_id"]),
        page=page,
        page_size=page_size,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/outcomes")
async def ai_outcomes(
    user: dict = Depends(current_user),
    date_from: Optional[str] = Query(default=None),
    date_to: Optional[str] = Query(default=None),
) -> dict:
    return await ai_analytics.outcomes(
        get_db(), tenant_id=str(user["_id"]), date_from=date_from, date_to=date_to
    )
