"""Production-ready structured logging (JSON in staging/production)."""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import settings
from app.security.audit import sanitize_error_message

_CONFIGURED = False


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": getattr(settings, "SERVICE_NAME", "ai-wa-chat-agent"),
            "env": settings.APP_ENV,
            "message": sanitize_error_message(record.getMessage(), max_len=1000),
        }
        for key in (
            "request_id",
            "route",
            "method",
            "status_code",
            "duration_ms",
            "user_id",
            "job_id",
            "campaign_id",
            "message_id",
            "twilio_error_code",
        ):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            # Never dump full traceback with secrets into message; keep type only for JSON
        return json.dumps(payload, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = []
        rid = getattr(record, "request_id", None)
        if rid:
            extras.append(f"request_id={rid}")
        route = getattr(record, "route", None)
        if route:
            extras.append(f"route={route}")
        if extras:
            return f"{base} {' '.join(extras)}"
        return base


def configure_logging(*, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return

    level_name = (settings.LOG_LEVEL or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    if settings.log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            _TextFormatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )
    root.addHandler(handler)

    # Quieter third-party noise
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    _CONFIGURED = True


def bind_extra(logger: logging.Logger, **kwargs: Any) -> logging.LoggerAdapter:
    safe = {
        k: v
        for k, v in kwargs.items()
        if k.lower() not in {"password", "token", "authorization", "secret", "api_key"}
    }
    return logging.LoggerAdapter(logger, safe)
