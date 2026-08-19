"""Temporary Meta Cloud API POC endpoints (authenticated test send)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.middleware.auth import current_user
from app.services import meta_whatsapp_service
from app.services.meta_whatsapp_service import MetaWhatsAppError

router = APIRouter(prefix="/meta", tags=["meta-poc"])
logger = logging.getLogger(__name__)


class MetaTestSendBody(BaseModel):
    to: str = Field(..., min_length=8, max_length=32)
    message: str = Field(..., min_length=1, max_length=4096)


@router.post("/test-send")
async def meta_test_send(
    body: MetaTestSendBody,
    user: dict = Depends(current_user),
) -> dict:
    """
    Phase 1 POC: send one text message via Meta Cloud API (no RQ).

    Requires JWT. Does not expose Meta secrets. Does not touch Twilio pipelines.
    """
    user_id = str(user.get("_id") or "")
    logger.info("meta_test_send requested user_id=%s to=%s text_len=%s", user_id, body.to, len(body.message))
    try:
        result = meta_whatsapp_service.send_text(to=body.to, text=body.message)
    except MetaWhatsAppError as exc:
        status = 502
        if exc.status_code and 400 <= int(exc.status_code) < 500:
            status = 400
        if "not configured" in str(exc).lower() or "invalid destination" in str(exc).lower():
            status = 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc

    return {
        "ok": True,
        "provider": result.provider,
        "provider_message_id": result.provider_message_id,
        "phone_number_id": result.phone_number_id,
        "to": result.to,
    }
