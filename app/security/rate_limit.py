"""Redis-backed fixed-window rate limiting."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import HTTPException, Request

from app.config import settings

logger = logging.getLogger(__name__)


def client_ip(request: Request) -> str:
    """Best-effort client IP; only trust X-Forwarded-For when proxies are configured."""
    if settings.TRUSTED_PROXY_COUNT > 0:
        forwarded = (request.headers.get("x-forwarded-for") or "").strip()
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            # Right-most trusted hop: take the leftmost untrusted = client
            if len(parts) >= settings.TRUSTED_PROXY_COUNT:
                idx = max(0, len(parts) - settings.TRUSTED_PROXY_COUNT)
                return parts[idx]
            if parts:
                return parts[0]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _redis():
    try:
        from app.workers.queue import get_redis

        return get_redis()
    except Exception:
        return None


def check_rate_limit(
    *,
    key: str,
    limit: int,
    window_sec: int,
) -> None:
    """Raise HTTP 429 when the key exceeds limit within the window. Fail-open if Redis down."""
    if limit <= 0 or window_sec <= 0:
        return
    r = _redis()
    if r is None:
        return
    full_key = f"rl:{key}"
    try:
        count = r.incr(full_key)
        if count == 1:
            r.expire(full_key, window_sec)
        if int(count) > limit:
            raise HTTPException(
                status_code=429,
                detail="Too many requests. Please try again later.",
            )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("Rate limiter unavailable: %s", type(exc).__name__)


def rate_limit_ip(request: Request, *, bucket: str, limit: int, window_sec: int) -> None:
    check_rate_limit(
        key=f"{bucket}:ip:{client_ip(request)}",
        limit=limit,
        window_sec=window_sec,
    )


def rate_limit_user(user_id: str, *, bucket: str, limit: int, window_sec: int) -> None:
    check_rate_limit(
        key=f"{bucket}:user:{user_id}",
        limit=limit,
        window_sec=window_sec,
    )


def rate_limit_auth(request: Request) -> None:
    rate_limit_ip(
        request,
        bucket="auth",
        limit=settings.RATE_LIMIT_AUTH_PER_IP,
        window_sec=settings.RATE_LIMIT_AUTH_WINDOW_SEC,
    )


def rate_limit_send(user_id: str) -> None:
    rate_limit_user(
        user_id,
        bucket="send",
        limit=settings.RATE_LIMIT_SEND_PER_USER,
        window_sec=settings.RATE_LIMIT_SEND_WINDOW_SEC,
    )


def rate_limit_upload(user_id: str) -> None:
    rate_limit_user(
        user_id,
        bucket="upload",
        limit=settings.RATE_LIMIT_UPLOAD_PER_USER,
        window_sec=settings.RATE_LIMIT_UPLOAD_WINDOW_SEC,
    )


def rate_limit_campaign(user_id: str) -> None:
    rate_limit_user(
        user_id,
        bucket="campaign",
        limit=settings.RATE_LIMIT_CAMPAIGN_PER_USER,
        window_sec=settings.RATE_LIMIT_CAMPAIGN_WINDOW_SEC,
    )


def rate_limit_webhook(request: Request, *, status_callback: bool = False) -> None:
    if status_callback:
        rate_limit_ip(
            request,
            bucket="twilio_status",
            limit=settings.RATE_LIMIT_STATUS_CALLBACK_PER_IP,
            window_sec=settings.RATE_LIMIT_STATUS_CALLBACK_WINDOW_SEC,
        )
    else:
        rate_limit_ip(
            request,
            bucket="twilio_webhook",
            limit=settings.RATE_LIMIT_WEBHOOK_PER_IP,
            window_sec=settings.RATE_LIMIT_WEBHOOK_WINDOW_SEC,
        )
