"""Optional Sentry integration — disabled when SENTRY_DSN is empty."""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)
_INITIALIZED = False


def init_sentry(*, service: str = "api") -> bool:
    """Initialise Sentry once. Returns True if enabled."""
    global _INITIALIZED
    dsn = (settings.SENTRY_DSN or "").strip()
    if not dsn:
        return False
    if _INITIALIZED:
        return True
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.redis import RedisIntegration
    except ImportError:
        logger.warning("SENTRY_DSN set but sentry-sdk is not installed")
        return False

    def _before_send(event, hint):
        # Drop expected client errors
        if "exc_info" in hint:
            exc = hint["exc_info"][1]
            status = getattr(exc, "status_code", None)
            if status and 400 <= int(status) < 500:
                return None
        # Redact common secret keys from request data
        req = event.get("request") or {}
        headers = req.get("headers") or {}
        for h in list(headers.keys()):
            if h.lower() in ("authorization", "cookie", "x-api-key"):
                headers[h] = "[REDACTED]"
        return event

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.sentry_environment,
        release=(settings.SENTRY_RELEASE or None) or None,
        traces_sample_rate=float(settings.SENTRY_TRACES_SAMPLE_RATE or 0),
        profiles_sample_rate=float(settings.SENTRY_PROFILES_SAMPLE_RATE or 0),
        send_default_pii=False,
        before_send=_before_send,
        integrations=[
            FastApiIntegration(transaction_style="endpoint"),
            LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            RedisIntegration(),
        ],
    )
    sentry_sdk.set_tag("service", service)
    _INITIALIZED = True
    logger.info("Sentry initialised for service=%s env=%s", service, settings.sentry_environment)
    return True


def capture_exception(exc: BaseException, **context: Optional[str]) -> None:
    if not (settings.SENTRY_DSN or "").strip():
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for k, v in context.items():
                if v is not None and k.lower() not in {"password", "token", "secret"}:
                    scope.set_tag(k, str(v)[:120])
            sentry_sdk.capture_exception(exc)
    except Exception:
        logger.debug("Sentry capture failed", exc_info=True)


def set_context(**kwargs) -> None:
    if not (settings.SENTRY_DSN or "").strip():
        return
    try:
        import sentry_sdk

        for k, v in kwargs.items():
            if v is not None:
                sentry_sdk.set_tag(k, str(v)[:120])
    except Exception:
        pass
