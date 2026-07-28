"""WhatsApp AI reply generator (thin facade over prompt + provider services)."""
from typing import Optional

from app.config import settings
from app.services.ai_config import resolve_ai_settings
from app.services.ai_prompt import (
    CORE_RULES,
    KIND_PLAYBOOKS,
    build_chat_messages,
    build_system_prompt as _build_system_prompt,
)
from app.services.ai_provider import chat_completion
from app.services.ai_quality import validate_output

__all__ = ["CORE_RULES", "KIND_PLAYBOOKS", "build_system_prompt", "generate_reply"]


def build_system_prompt(agent: Optional[dict], company: Optional[str] = None) -> str:
    """Backward-compatible wrapper."""
    return _build_system_prompt(agent=agent, company=company, ai_settings=resolve_ai_settings(None))


def generate_reply(
    agent: Optional[dict],
    history: list[dict],
    company: Optional[str] = None,
    *,
    tenant_id: Optional[str] = None,
    lead: Optional[dict] = None,
    ai_settings: Optional[dict] = None,
    summary: Optional[str] = None,
    user: Optional[dict] = None,
) -> str:
    ai = ai_settings or resolve_ai_settings(user)
    if not ai.get("enabled"):
        return ""

    system = _build_system_prompt(
        agent=agent,
        company=company,
        ai_settings=ai,
        lead_profile=lead,
        conversation_summary=summary,
        language=(lead or {}).get("language") or ai.get("default_language"),
        message_purpose="support",
    )
    ctx_msgs = []
    for m in history[-int(settings.AI_MAX_CONTEXT_MESSAGES or settings.OPENAI_MAX_HISTORY) :]:
        if "role" in m and "content" in m:
            ctx_msgs.append({"role": m["role"], "content": m.get("content") or ""})
        else:
            role = "user" if m.get("direction") == "inbound" else "assistant"
            ctx_msgs.append({"role": role, "content": m.get("message") or ""})

    messages = build_chat_messages(system=system, context_messages=ctx_msgs)
    lead_oid = (lead or {}).get("_id") or (lead or {}).get("id")
    result = chat_completion(
        messages=messages,
        model=ai["model"],
        fallback_model=ai["fallback_model"],
        temperature=float(ai["temperature"]),
        max_tokens=int(ai["max_output_tokens"]),
        tenant_id=tenant_id,
        operation="reply",
        conversation_id=str(lead_oid) if lead_oid else None,
    )
    if not result.success:
        raise RuntimeError(result.error_category or "provider_error")

    floor = (agent or {}).get("price_floor")
    ceil = (agent or {}).get("price_ceiling")
    try:
        floor_f = float(floor) if floor is not None else None
    except (TypeError, ValueError):
        floor_f = None
    try:
        ceil_f = float(ceil) if ceil is not None else None
    except (TypeError, ValueError):
        ceil_f = None

    quality = validate_output(
        result.text,
        disallowed_topics=ai.get("ai_disallowed_topics") or "",
        price_floor=floor_f,
        price_ceiling=ceil_f,
    )
    if not quality.ok:
        raise RuntimeError(quality.reason or "quality_rejected")
    return quality.text
