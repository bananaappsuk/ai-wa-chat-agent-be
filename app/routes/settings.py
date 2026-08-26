"""Account / WhatsApp / AI configuration status (no secrets)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.middleware.security import get_request_id
from app.models.common import utcnow
from app.security.audit import audit
from app.security.permissions import require_permission
from app.security.rate_limit import rate_limit_user
from app.services.ai_config import (
    PATCH_ALLOW,
    public_ai_settings,
    resolve_ai_settings,
    sanitize_text,
    validate_model,
)
from app.services.ai_prompt import build_chat_messages, build_system_prompt
from app.services.ai_provider import chat_completion
from app.services.ai_quality import validate_output
from app.services.ai_quota import usage_snapshot
from app.services.activity import record_activity
from app.services.whatsapp_status import whatsapp_status_payload
from app.services.whatsapp_settings import apply_whatsapp_settings_patch, build_whatsapp_settings
from bson import ObjectId

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("/whatsapp-status")
async def whatsapp_status(_user: dict = Depends(current_user)) -> dict:
    """Legacy Twilio platform health (unchanged contract)."""
    return whatsapp_status_payload()


@router.get("/whatsapp")
async def get_whatsapp_settings(user: dict = Depends(current_user)) -> dict:
    return build_whatsapp_settings(user)


class TwilioWhatsAppPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    routing_number: str | None = Field(default=None, max_length=32)


class MetaWhatsAppPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone_number_id: str | None = Field(default=None, max_length=32)
    waba_id: str | None = Field(default=None, max_length=32)
    display_phone_number: str | None = Field(default=None, max_length=32)


class WhatsAppSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    twilio: TwilioWhatsAppPatch | None = None
    meta: MetaWhatsAppPatch | None = None
    provider: str | None = Field(default=None, max_length=20)
    access_token: str | None = None
    app_secret: str | None = None
    auth_token: str | None = None
    account_sid: str | None = None
    webhook_verify_token: str | None = None


@router.patch("/whatsapp")
async def patch_whatsapp_settings(
    body: WhatsAppSettingsPatch, user: dict = Depends(current_user)
) -> dict:
    require_permission(user, "change_account_settings")
    if any(
        getattr(body, k) is not None
        for k in (
            "provider",
            "access_token",
            "app_secret",
            "auth_token",
            "account_sid",
            "webhook_verify_token",
        )
    ):
        raise HTTPException(status_code=400, detail="Unsupported fields")
    raw = body.model_dump(exclude_unset=True)
    raw.pop("provider", None)
    data = {k: v for k, v in raw.items() if k in ("twilio", "meta") and v is not None}
    if not data:
        raise HTTPException(status_code=400, detail="No valid fields")
    result = await apply_whatsapp_settings_patch(get_db(), user, data)
    audit(
        "settings.whatsapp_update",
        user_id=str(user["_id"]),
        request_id=get_request_id(),
        extra={"fields": result["changed"]},
    )
    await record_activity(
        get_db(),
        tenant_id=str(user["_id"]),
        event_type="settings.whatsapp_updated",
        summary="WhatsApp integration settings updated",
        actor_id=str(user["_id"]),
        resource_type="settings",
        resource_id="whatsapp",
        metadata={"fields": ",".join(result["changed"])},
    )
    fresh = await get_db().users.find_one({"_id": ObjectId(user["_id"])})
    return build_whatsapp_settings(fresh or user)


class MetaOnboardingCompleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str = Field(..., min_length=8, max_length=128)
    code: str = Field(..., min_length=8, max_length=4096)
    waba_id: str = Field(..., min_length=5, max_length=32)
    phone_number_id: str = Field(..., min_length=5, max_length=32)
    display_phone_number: str | None = Field(default=None, max_length=32)
    business_id: str | None = Field(default=None, max_length=32)


@router.post("/whatsapp/meta/onboarding/start")
async def start_meta_onboarding(user: dict = Depends(current_user)) -> dict:
    require_permission(user, "change_account_settings")
    rate_limit_user(
        str(user["_id"]),
        bucket="meta_onboarding_start",
        limit=10,
        window_sec=60,
    )
    from app.services.meta_onboarding import start_onboarding_session

    out = start_onboarding_session(user_id=str(user["_id"]))
    audit(
        "settings.meta_onboarding_started",
        user_id=str(user["_id"]),
        request_id=get_request_id(),
        extra={"graph_version": out.get("graph_version")},
    )
    await record_activity(
        get_db(),
        tenant_id=str(user["_id"]),
        event_type="settings.meta_onboarding_started",
        summary="Meta WhatsApp onboarding started",
        actor_id=str(user["_id"]),
        resource_type="settings",
        resource_id="whatsapp",
    )
    return out


@router.post("/whatsapp/meta/onboarding/complete")
async def complete_meta_onboarding(
    body: MetaOnboardingCompleteBody, user: dict = Depends(current_user)
) -> dict:
    require_permission(user, "change_account_settings")
    from app.services.entitlements import require_can_connect_number

    require_can_connect_number(user)
    rate_limit_user(
        str(user["_id"]),
        bucket="meta_onboarding_complete",
        limit=10,
        window_sec=60,
    )
    from app.services.meta_onboarding import complete_onboarding

    try:
        result = await complete_onboarding(
            get_db(),
            user=user,
            state=body.state,
            code=body.code,
            waba_id=body.waba_id,
            phone_number_id=body.phone_number_id,
            display_phone_number=body.display_phone_number,
            business_id=body.business_id,
        )
    except HTTPException as exc:
        audit(
            "settings.meta_onboarding_failed",
            user_id=str(user["_id"]),
            request_id=get_request_id(),
            result="error",
            extra={"status": exc.status_code},
        )
        raise
    audit(
        result["audit_action"],
        user_id=str(user["_id"]),
        request_id=get_request_id(),
        extra={"status": result["status"], **(result.get("masked") or {})},
    )
    await record_activity(
        get_db(),
        tenant_id=str(user["_id"]),
        event_type=result["audit_action"],
        summary="Meta WhatsApp connected" if result["ok"] else "Meta WhatsApp onboarding needs attention",
        actor_id=str(user["_id"]),
        resource_type="settings",
        resource_id="whatsapp",
        metadata={"status": result["status"]},
    )
    fresh = await get_db().users.find_one({"_id": ObjectId(user["_id"])})
    payload = build_whatsapp_settings(fresh or user)
    payload["onboarding"] = {"ok": result["ok"], "status": result["status"], "warnings": result.get("warnings") or []}
    return payload


@router.post("/whatsapp/meta/disconnect")
async def disconnect_meta_whatsapp(user: dict = Depends(current_user)) -> dict:
    require_permission(user, "change_account_settings")
    rate_limit_user(
        str(user["_id"]),
        bucket="meta_disconnect",
        limit=10,
        window_sec=60,
    )
    from app.services.meta_onboarding import disconnect_meta

    result = await disconnect_meta(get_db(), user=user)
    audit(
        "settings.meta_disconnected",
        user_id=str(user["_id"]),
        request_id=get_request_id(),
        extra=result.get("masked") or {},
    )
    await record_activity(
        get_db(),
        tenant_id=str(user["_id"]),
        event_type="settings.meta_disconnected",
        summary="Meta WhatsApp disconnected",
        actor_id=str(user["_id"]),
        resource_type="settings",
        resource_id="whatsapp",
    )
    fresh = await get_db().users.find_one({"_id": ObjectId(user["_id"])})
    return build_whatsapp_settings(fresh or user)


@router.get("/ai")
async def get_ai_settings(user: dict = Depends(current_user)) -> dict:
    require_permission(user, "change_account_settings")
    out = public_ai_settings(user)
    out["usage"] = usage_snapshot(str(user["_id"]))
    return out


class AISettingsPatch(BaseModel):
    enabled: bool | None = None
    model: str | None = Field(default=None, max_length=64)
    fallback_model: str | None = Field(default=None, max_length=64)
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int | None = Field(default=None, ge=50, le=2000)
    summaries_enabled: bool | None = None
    extraction_enabled: bool | None = None
    moderation_enabled: bool | None = None
    analytics_enabled: bool | None = None
    ai_business_description: str | None = Field(default=None, max_length=2000)
    ai_tone: str | None = Field(default=None, max_length=40)
    ai_custom_instructions: str | None = Field(default=None, max_length=4000)
    ai_disallowed_topics: str | None = Field(default=None, max_length=1000)
    ai_escalation_rules: str | None = Field(default=None, max_length=2000)


@router.patch("/ai")
async def patch_ai_settings(
    body: AISettingsPatch, user: dict = Depends(current_user)
) -> dict:
    require_permission(user, "change_account_settings")
    raw = body.model_dump(exclude_unset=True)
    data = {k: v for k, v in raw.items() if k in PATCH_ALLOW}
    if not data:
        raise HTTPException(status_code=400, detail="No valid fields")
    if "model" in data and data["model"]:
        try:
            data["model"] = validate_model(data["model"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "fallback_model" in data and data["fallback_model"]:
        try:
            data["fallback_model"] = validate_model(data["fallback_model"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    for key in (
        "ai_business_description",
        "ai_tone",
        "ai_custom_instructions",
        "ai_disallowed_topics",
        "ai_escalation_rules",
    ):
        if key in data and data[key] is not None:
            limits = {
                "ai_business_description": 2000,
                "ai_tone": 40,
                "ai_custom_instructions": 4000,
                "ai_disallowed_topics": 1000,
                "ai_escalation_rules": 2000,
            }
            data[key] = sanitize_text(str(data[key]), max_len=limits[key])

    current = dict(user.get("ai_settings") or {})
    current.update(data)
    await get_db().users.update_one(
        {"_id": ObjectId(user["_id"])},
        {"$set": {"ai_settings": current, "updated_at": utcnow()}},
    )
    audit(
        "settings.ai_update",
        user_id=str(user["_id"]),
        request_id=get_request_id(),
        extra={"fields": sorted(data.keys())},
    )
    await record_activity(
        get_db(),
        tenant_id=str(user["_id"]),
        event_type="settings.ai_updated",
        summary="AI settings updated",
        actor_id=str(user["_id"]),
        resource_type="settings",
        resource_id="ai",
        metadata={"fields": sorted(data.keys())},
    )
    fresh = await get_db().users.find_one({"_id": ObjectId(user["_id"])})
    out = public_ai_settings(fresh or user)
    out["usage"] = usage_snapshot(str(user["_id"]))
    return out


class AITestBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=1000)
    conversation_id: str | None = Field(default=None, max_length=40)


@router.post("/ai/test")
async def test_ai(
    body: AITestBody,
    request: Request,
    user: dict = Depends(current_user),
) -> dict:
    """Safe AI test — does not send WhatsApp."""
    require_permission(user, "change_account_settings")
    rate_limit_user(
        str(user["_id"]),
        bucket="ai_test",
        limit=max(1, int(settings.AI_TEST_RATE_LIMIT_PER_USER)),
        window_sec=60,
    )
    ai = resolve_ai_settings(user)
    if not ai["enabled"]:
        raise HTTPException(status_code=400, detail="AI is disabled")

    system = build_system_prompt(
        agent=None,
        company=user.get("company_name"),
        ai_settings=ai,
        message_purpose="test",
    )
    msgs = build_chat_messages(
        system=system,
        context_messages=[{"role": "user", "content": body.prompt}],
    )
    # Optional conversation context — same tenant only
    if body.conversation_id:
        from app.security.validation import require_object_id
        from app.services.ai_context import load_conversation_context

        require_object_id(body.conversation_id)
        lead = await get_db().leads.find_one(
            {"_id": ObjectId(body.conversation_id), "user_id": str(user["_id"])}
        )
        if not lead:
            raise HTTPException(status_code=404, detail="Conversation not found")
        ctx = load_conversation_context(
            get_db(),
            tenant_id=str(user["_id"]),
            lead_id=body.conversation_id,
        )
        msgs = build_chat_messages(system=system, context_messages=ctx["messages"][-8:])
        msgs.append(
            {
                "role": "user",
                "content": f"<<<USER_MESSAGE>>>\n{body.prompt}\n<<<END_USER_MESSAGE>>>",
            }
        )

    result = chat_completion(
        messages=msgs,
        model=ai["model"],
        fallback_model=ai["fallback_model"],
        temperature=ai["temperature"],
        max_tokens=min(400, ai["max_output_tokens"]),
        tenant_id=str(user["_id"]),
        operation="reply",
        conversation_id=body.conversation_id,
    )
    quality = validate_output(result.text, disallowed_topics=ai.get("ai_disallowed_topics") or "")
    from app.services.ai_moderation import moderate_outbound

    mod = moderate_outbound(result.text or "", disallowed_topics=ai.get("ai_disallowed_topics") or "")
    audit(
        "settings.ai_test",
        user_id=str(user["_id"]),
        request_id=get_request_id(),
        extra={"success": result.success, "model": result.model},
    )
    return {
        "success": result.success and quality.ok and mod.allowed,
        "response": quality.text if quality.ok else "",
        "latency_ms": result.latency_ms,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "error_category": result.error_category or (None if quality.ok else quality.reason),
        "moderation": {"allowed": mod.allowed, "categories": mod.categories},
        "used_fallback": result.used_fallback,
    }
