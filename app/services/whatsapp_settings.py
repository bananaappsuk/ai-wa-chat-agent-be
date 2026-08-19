"""Tenant-facing WhatsApp integration status (Phase 2H). No secrets, no send routing."""
from __future__ import annotations

import re
from typing import Any, Optional

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.models.common import utcnow
from app.services.whatsapp_status import detect_sender_type, whatsapp_status_payload

_GRAPH_ID = re.compile(r"^[0-9]{5,32}$")
_SECRET_KEYS = (
    "access_token",
    "app_secret",
    "auth_token",
    "account_sid",
    "webhook_verify_token",
    "jwt_secret",
    "META_ACCESS_TOKEN",
    "META_APP_SECRET",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_ACCOUNT_SID",
)


def _iso(val: Any) -> Optional[str]:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    text = str(val).strip()
    return text or None


def _mask_id(raw: Optional[str]) -> Optional[str]:
    text = (raw or "").strip()
    if not text:
        return None
    if len(text) <= 4:
        return "****"
    return f"...{text[-4:]}"


def validate_graph_id(raw: Optional[str], *, field: str) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if not _GRAPH_ID.fullmatch(text):
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be a numeric WhatsApp Cloud ID (5–32 digits).",
        )
    return text


def _twilio_slice(user: dict) -> dict[str, Any]:
    routing = (user.get("twilio_whatsapp_to") or "").strip() or None
    routing_ready = bool(routing)
    platform = whatsapp_status_payload()
    platform_sender_ready = bool(platform.get("sender_configured"))
    warnings = list(platform.get("warnings") or [])
    if not routing_ready:
        warnings.append("No Twilio inbound routing number is linked to this account")

    if not routing_ready:
        status = "not_configured"
    elif not platform_sender_ready:
        status = "misconfigured"
    else:
        status = "connected"

    return {
        "status": status,
        "enabled": routing_ready,
        "configured": status == "connected",
        "routing_number": routing,
        "routing_ready": routing_ready,
        "platform_sender_ready": platform_sender_ready,
        "sender_type": detect_sender_type(),
        "status_callback_configured": bool(platform.get("status_callback_configured")),
        "warnings": warnings,
    }


def _meta_slice(user: dict) -> dict[str, Any]:
    from app.services.meta_credentials import tenant_meta_ready

    pnid = str(user.get("meta_phone_number_id") or "").strip() or None
    waba = str(user.get("meta_waba_id") or "").strip() or None
    display = str(user.get("meta_display_phone_number") or "").strip() or None
    conn = str(user.get("meta_connection_status") or "").strip().lower() or None
    token_valid = bool(pnid and tenant_meta_ready(user))
    routing_ready = bool(pnid)
    sending_ready = token_valid
    warnings: list[str] = []
    if not pnid:
        warnings.append("No Meta phone number ID is linked to this account")
    elif conn in (None, "", "disconnected", "pending", "error"):
        warnings.append("Meta WhatsApp is not connected for this account")
    elif not token_valid:
        warnings.append("Meta credentials are missing or invalid for this account")
    if pnid and not waba:
        warnings.append("Meta WABA ID is not set")

    if not routing_ready:
        status = "not_configured"
    elif conn == "error":
        status = "misconfigured"
    elif not token_valid:
        status = "requires_action"
    else:
        status = "connected"

    return {
        "status": status,
        "enabled": routing_ready,
        "configured": status == "connected",
        "connection_status": conn,
        "phone_number_id": pnid,
        "waba_id": waba,
        "display_phone_number": display,
        "routing_ready": routing_ready,
        "sending_ready": sending_ready,
        "token_valid": token_valid,
        "token_expires_at": _iso(user.get("meta_token_expires_at")),
        "webhook_ready": None,
        "last_template_sync_at": _iso(user.get("meta_last_template_sync_at")),
        "warnings": warnings,
    }


def build_whatsapp_settings(user: Optional[dict]) -> dict[str, Any]:
    """Safe serializer — never includes provider secrets."""
    doc = user or {}
    out = {
        "twilio": _twilio_slice(doc),
        "meta": _meta_slice(doc),
        "embedded_signup": {"available": False},
    }
    blob_keys = set(out.keys()) | set(out["twilio"].keys()) | set(out["meta"].keys())
    for needle in ("access_token", "app_secret", "auth_token", "account_sid"):
        if needle in blob_keys:
            raise RuntimeError("Refusing to serialize WhatsApp settings with secret-like keys")
    return out


async def apply_whatsapp_settings_patch(db, user: dict, data: dict[str, Any]) -> dict[str, Any]:
    """Update tenant routing identifiers only. Does not change send routing logic."""
    from app.routes.profile import normalize_twilio_whatsapp_to

    update: dict[str, Any] = {}
    changed: list[str] = []
    twilio = data.get("twilio") or {}
    meta = data.get("meta") or {}

    if "routing_number" in twilio:
        raw_routing = twilio.get("routing_number")
        if raw_routing is not None and str(raw_routing).strip():
            normalized = normalize_twilio_whatsapp_to(raw_routing)
            if not normalized:
                raise HTTPException(
                    status_code=400,
                    detail="Business WhatsApp number must be international format",
                )
        else:
            normalized = None
        update["twilio_whatsapp_to"] = normalized
        changed.append("twilio.routing_number")
        if normalized:
            clash = await db.users.find_one(
                {"twilio_whatsapp_to": normalized, "_id": {"$ne": ObjectId(user["_id"])}}
            )
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail="That WhatsApp number is already linked",
                )

    if "phone_number_id" in meta:
        pnid = validate_graph_id(meta.get("phone_number_id"), field="phone_number_id")
        update["meta_phone_number_id"] = pnid
        changed.append("meta.phone_number_id")
        if pnid:
            clash = await db.users.find_one(
                {"meta_phone_number_id": pnid, "_id": {"$ne": ObjectId(user["_id"])}}
            )
            if clash:
                raise HTTPException(
                    status_code=409,
                    detail="That WhatsApp number is already linked",
                )

    if "waba_id" in meta:
        update["meta_waba_id"] = validate_graph_id(meta.get("waba_id"), field="waba_id")
        changed.append("meta.waba_id")

    if "display_phone_number" in meta:
        from app.services.meta_whatsapp_service import normalize_meta_phone

        raw_display = meta.get("display_phone_number")
        if raw_display is None or not str(raw_display).strip():
            update["meta_display_phone_number"] = None
        else:
            display = normalize_meta_phone(raw_display)
            if not display:
                raise HTTPException(
                    status_code=400,
                    detail="display_phone_number must be international format, e.g. +447700900000",
                )
            update["meta_display_phone_number"] = display
        changed.append("meta.display_phone_number")

    if not update:
        raise HTTPException(status_code=400, detail="No valid fields")

    update["updated_at"] = utcnow()
    try:
        await db.users.update_one({"_id": ObjectId(user["_id"])}, {"$set": update})
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409,
            detail="That WhatsApp number is already linked",
        ) from exc

    return {
        "changed": changed,
        "masked": {
            "twilio.routing_number": _mask_id(update.get("twilio_whatsapp_to"))
            if "twilio_whatsapp_to" in update
            else None,
            "meta.phone_number_id": _mask_id(update.get("meta_phone_number_id"))
            if "meta_phone_number_id" in update
            else None,
        },
    }
