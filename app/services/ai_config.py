"""Resolve AI configuration (env defaults + tenant overrides). Never exposes API keys."""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.config import settings

_SAFE_MODEL = re.compile(r"^[a-zA-Z0-9._-]{2,64}$")

DEFAULT_TENANT_AI = {
    "enabled": True,
    "model": None,  # inherit env
    "fallback_model": None,
    "temperature": None,
    "max_output_tokens": None,
    "summaries_enabled": None,
    "extraction_enabled": None,
    "moderation_enabled": None,
    "analytics_enabled": None,
    "ai_business_description": "",
    "ai_tone": "neutral",
    "ai_custom_instructions": "",
    "ai_disallowed_topics": "",
    "ai_escalation_rules": "",
}


def allowed_models() -> set[str]:
    raw = (settings.AI_ALLOWED_MODELS or "").strip()
    return {m.strip() for m in raw.split(",") if m.strip() and _SAFE_MODEL.match(m.strip())}


def validate_model(name: Optional[str]) -> str:
    if not name or not str(name).strip():
        raise ValueError("Model is required")
    model = str(name).strip()
    allowed = allowed_models()
    if allowed and model not in allowed:
        raise ValueError(f"Model not allowed: {model}")
    if not _SAFE_MODEL.match(model):
        raise ValueError("Invalid model name")
    return model


def parse_pricing() -> dict[str, dict[str, float]]:
    try:
        data = json.loads(settings.AI_MODEL_PRICING_JSON or "{}")
        out: dict[str, dict[str, float]] = {}
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(v, dict):
                    out[str(k)] = {
                        "input_per_1m": float(v.get("input_per_1m") or 0),
                        "output_per_1m": float(v.get("output_per_1m") or 0),
                    }
        return out
    except Exception:
        return {}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = parse_pricing().get(model) or {"input_per_1m": 0.15, "output_per_1m": 0.60}
    return round(
        (max(0, input_tokens) / 1_000_000) * pricing["input_per_1m"]
        + (max(0, output_tokens) / 1_000_000) * pricing["output_per_1m"],
        6,
    )


def sanitize_text(value: Optional[str], *, max_len: int) -> str:
    text = (value or "").strip()
    # Strip control chars except newline/tab
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return text[:max_len]


def tenant_ai_doc(user: dict) -> dict[str, Any]:
    raw = user.get("ai_settings") if isinstance(user.get("ai_settings"), dict) else {}
    return {**DEFAULT_TENANT_AI, **{k: v for k, v in raw.items() if k in DEFAULT_TENANT_AI}}


def resolve_ai_settings(user: Optional[dict] = None) -> dict[str, Any]:
    """Merged effective settings for a tenant. No secrets."""
    t = tenant_ai_doc(user or {})
    global_on = bool(settings.AI_FEATURES_ENABLED) and bool((settings.OPENAI_API_KEY or "").strip())
    tenant_on = t.get("enabled") is not False
    model = t.get("model") or settings.OPENAI_MODEL
    fallback = t.get("fallback_model") or settings.OPENAI_FALLBACK_MODEL or model
    try:
        model = validate_model(model)
    except ValueError:
        model = settings.OPENAI_MODEL
    try:
        fallback = validate_model(fallback)
    except ValueError:
        fallback = model

    temp = t.get("temperature")
    if temp is None:
        temp = float(settings.OPENAI_TEMPERATURE)
    max_out = t.get("max_output_tokens")
    if max_out is None:
        max_out = int(settings.OPENAI_MAX_OUTPUT_TOKENS)

    def _flag(key: str, env_default: bool) -> bool:
        v = t.get(key)
        return bool(env_default if v is None else v)

    return {
        "enabled": global_on and tenant_on,
        "globally_enabled": bool(settings.AI_FEATURES_ENABLED),
        "api_key_configured": bool((settings.OPENAI_API_KEY or "").strip()),
        "model": model,
        "fallback_model": fallback,
        "temperature": max(0.0, min(2.0, float(temp))),
        "max_output_tokens": max(50, min(2000, int(max_out))),
        "summaries_enabled": _flag("summaries_enabled", settings.AI_SUMMARIES_ENABLED),
        "extraction_enabled": _flag("extraction_enabled", settings.AI_EXTRACTION_ENABLED),
        "moderation_enabled": _flag("moderation_enabled", settings.AI_MODERATION_ENABLED),
        "analytics_enabled": _flag("analytics_enabled", settings.AI_ANALYTICS_ENABLED),
        "daily_token_limit": int(settings.AI_DAILY_TOKEN_LIMIT_PER_TENANT),
        "monthly_cost_limit": float(settings.AI_MONTHLY_COST_LIMIT_PER_TENANT),
        "requests_per_minute_limit": int(settings.AI_MAX_REQUESTS_PER_MINUTE_PER_TENANT),
        "ai_business_description": sanitize_text(t.get("ai_business_description"), max_len=2000),
        "ai_tone": sanitize_text(t.get("ai_tone") or "neutral", max_len=40),
        "ai_custom_instructions": sanitize_text(t.get("ai_custom_instructions"), max_len=4000),
        "ai_disallowed_topics": sanitize_text(t.get("ai_disallowed_topics"), max_len=1000),
        "ai_escalation_rules": sanitize_text(t.get("ai_escalation_rules"), max_len=2000),
        "allowed_models": sorted(allowed_models()),
        "default_language": settings.AI_DEFAULT_LANGUAGE,
    }


def public_ai_settings(user: Optional[dict] = None) -> dict[str, Any]:
    """Safe payload for GET /settings/ai — no credentials."""
    s = resolve_ai_settings(user)
    return {
        "enabled": s["enabled"],
        "globally_enabled": s["globally_enabled"],
        "api_key_configured": s["api_key_configured"],
        "model": s["model"],
        "fallback_model": s["fallback_model"],
        "temperature": s["temperature"],
        "max_output_tokens": s["max_output_tokens"],
        "summaries_enabled": s["summaries_enabled"],
        "extraction_enabled": s["extraction_enabled"],
        "moderation_enabled": s["moderation_enabled"],
        "analytics_enabled": s["analytics_enabled"],
        "daily_token_limit": s["daily_token_limit"],
        "monthly_cost_limit": s["monthly_cost_limit"],
        "requests_per_minute_limit": s["requests_per_minute_limit"],
        "ai_business_description": s["ai_business_description"],
        "ai_tone": s["ai_tone"],
        "ai_custom_instructions": s["ai_custom_instructions"],
        "ai_disallowed_topics": s["ai_disallowed_topics"],
        "ai_escalation_rules": s["ai_escalation_rules"],
        "allowed_models": s["allowed_models"],
    }


PATCH_ALLOW = frozenset(
    {
        "enabled",
        "model",
        "fallback_model",
        "temperature",
        "max_output_tokens",
        "summaries_enabled",
        "extraction_enabled",
        "moderation_enabled",
        "analytics_enabled",
        "ai_business_description",
        "ai_tone",
        "ai_custom_instructions",
        "ai_disallowed_topics",
        "ai_escalation_rules",
    }
)
