"""AI usage tracking and Redis quota enforcement."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.services.ai_config import estimate_cost
from app.services.notifications import create_notification, create_notification_sync

logger = logging.getLogger(__name__)


def _redis():
    from redis import Redis

    return Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _period_keys(tenant_id: str) -> tuple[str, str, str]:
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y%m%d")
    month = now.strftime("%Y%m")
    return (
        f"ai:rpm:{tenant_id}:{now.strftime('%Y%m%d%H%M')}",
        f"ai:tokens:day:{tenant_id}:{day}",
        f"ai:cost:month:{tenant_id}:{month}",
    )


def check_quota(tenant_id: str) -> tuple[bool, Optional[str]]:
    """Return (allowed, block_reason). Fail-open on Redis errors for human messaging path,
    but AI calls should treat Redis errors as allowed with logging (or fail closed).
    Spec: fail safely when quota exceeded; don't call provider after blocking.
    """
    rpm_limit = int(settings.AI_MAX_REQUESTS_PER_MINUTE_PER_TENANT or 0)
    day_limit = int(settings.AI_DAILY_TOKEN_LIMIT_PER_TENANT or 0)
    month_limit = float(settings.AI_MONTHLY_COST_LIMIT_PER_TENANT or 0)
    if rpm_limit <= 0 and day_limit <= 0 and month_limit <= 0:
        return True, None
    try:
        r = _redis()
        rpm_k, day_k, cost_k = _period_keys(tenant_id)
        if rpm_limit > 0:
            n = int(r.get(rpm_k) or 0)
            if n >= rpm_limit:
                return False, "quota_exceeded"
        if day_limit > 0:
            toks = int(r.get(day_k) or 0)
            if toks >= day_limit:
                return False, "quota_exceeded"
        if month_limit > 0:
            cost = float(r.get(cost_k) or 0)
            if cost >= month_limit:
                return False, "quota_exceeded"
        return True, None
    except Exception:
        logger.warning("AI quota check failed open", exc_info=True)
        return True, None


def record_quota_usage(tenant_id: str, *, tokens: int, cost: float) -> None:
    try:
        r = _redis()
        rpm_k, day_k, cost_k = _period_keys(tenant_id)
        pipe = r.pipeline()
        pipe.incr(rpm_k)
        pipe.expire(rpm_k, 120)
        if tokens:
            pipe.incrby(day_k, int(tokens))
            pipe.expire(day_k, 86400 * 2)
        if cost:
            pipe.incrbyfloat(cost_k, float(cost))
            pipe.expire(cost_k, 86400 * 40)
        pipe.execute()
        _maybe_notify_thresholds(r, tenant_id)
    except Exception:
        logger.warning("AI quota record failed", exc_info=True)


def _maybe_notify_thresholds(r, tenant_id: str) -> None:
    day_limit = int(settings.AI_DAILY_TOKEN_LIMIT_PER_TENANT or 0)
    month_limit = float(settings.AI_MONTHLY_COST_LIMIT_PER_TENANT or 0)
    _, day_k, cost_k = _period_keys(tenant_id)
    day_used = int(r.get(day_k) or 0)
    cost_used = float(r.get(cost_k) or 0)

    def _pct(used: float, limit: float) -> Optional[int]:
        if limit <= 0:
            return None
        ratio = used / limit
        if ratio >= 1.0:
            return 100
        if ratio >= 0.9:
            return 90
        if ratio >= 0.75:
            return 75
        return None

    for kind, pct in (
        ("daily_tokens", _pct(day_used, day_limit)),
        ("monthly_cost", _pct(cost_used, month_limit)),
    ):
        if pct is None:
            continue
        dedupe = f"ai_quota:{tenant_id}:{kind}:{pct}:{datetime.now(timezone.utc).strftime('%Y%m%d')}"
        try:
            create_notification_sync(
                # sync mongo via pymongo in worker; for async routes use async path
                _sync_db(),
                user_id=tenant_id,
                type="system",
                title=f"AI usage at {pct}%",
                message=f"Your AI {kind.replace('_', ' ')} usage reached {pct}% of the limit.",
                resource_type="ai_quota",
                resource_id=kind,
                dedupe_key=dedupe,
            )
        except Exception:
            pass


def _sync_db():
    from pymongo import MongoClient

    return MongoClient(settings.MONGO_URI)[settings.MONGO_DB]


async def record_usage_async(
    db,
    *,
    tenant_id: str,
    operation: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    success: bool = True,
    error_category: Optional[str] = None,
    conversation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    total = int(input_tokens) + int(output_tokens)
    cost = estimate_cost(model, int(input_tokens), int(output_tokens))
    doc = {
        "tenant_id": tenant_id,
        "user_id": user_id or tenant_id,
        "conversation_id": conversation_id,
        "model": model,
        "operation": operation,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": total,
        "estimated_cost": cost,
        "latency_ms": int(latency_ms),
        "success": bool(success),
        "error_category": error_category,
        "metadata": {k: v for k, v in (metadata or {}).items() if k not in ("prompt", "body", "message")},
        "created_at": datetime.now(timezone.utc),
    }
    res = await db.ai_usage.insert_one(doc)
    doc["_id"] = res.inserted_id
    if success:
        record_quota_usage(tenant_id, tokens=total, cost=cost)
    return doc


def record_usage_sync(
    db,
    *,
    tenant_id: str,
    operation: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    success: bool = True,
    error_category: Optional[str] = None,
    conversation_id: Optional[str] = None,
    user_id: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    total = int(input_tokens) + int(output_tokens)
    cost = estimate_cost(model, int(input_tokens), int(output_tokens))
    doc = {
        "tenant_id": tenant_id,
        "user_id": user_id or tenant_id,
        "conversation_id": conversation_id,
        "model": model,
        "operation": operation,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": total,
        "estimated_cost": cost,
        "latency_ms": int(latency_ms),
        "success": bool(success),
        "error_category": error_category,
        "metadata": {k: v for k, v in (metadata or {}).items() if k not in ("prompt", "body", "message")},
        "created_at": datetime.now(timezone.utc),
    }
    res = db.ai_usage.insert_one(doc)
    doc["_id"] = res.inserted_id
    if success:
        record_quota_usage(tenant_id, tokens=total, cost=cost)
    return doc


def usage_snapshot(tenant_id: str) -> dict[str, Any]:
    try:
        r = _redis()
        rpm_k, day_k, cost_k = _period_keys(tenant_id)
        return {
            "requests_this_minute": int(r.get(rpm_k) or 0),
            "tokens_today": int(r.get(day_k) or 0),
            "cost_this_month": float(r.get(cost_k) or 0),
            "daily_token_limit": int(settings.AI_DAILY_TOKEN_LIMIT_PER_TENANT),
            "monthly_cost_limit": float(settings.AI_MONTHLY_COST_LIMIT_PER_TENANT),
            "requests_per_minute_limit": int(settings.AI_MAX_REQUESTS_PER_MINUTE_PER_TENANT),
        }
    except Exception:
        return {
            "requests_this_minute": 0,
            "tokens_today": 0,
            "cost_this_month": 0.0,
            "daily_token_limit": int(settings.AI_DAILY_TOKEN_LIMIT_PER_TENANT),
            "monthly_cost_limit": float(settings.AI_MONTHLY_COST_LIMIT_PER_TENANT),
            "requests_per_minute_limit": int(settings.AI_MAX_REQUESTS_PER_MINUTE_PER_TENANT),
        }
