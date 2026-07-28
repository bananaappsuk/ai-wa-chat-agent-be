"""Structured logging helpers + minimal audit trail (no secrets / message bodies)."""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.models.common import utcnow

logger = logging.getLogger("app.audit")

_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[a-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)(jwt[_-]?secret|auth[_-]?token|api[_-]?key|password)([\"'=:\s]+)([^\s,\"']+)"),
)


def mask_phone(phone: Optional[str]) -> str:
    raw = (phone or "").strip()
    if len(raw) < 6:
        return "***"
    return f"{raw[:3]}***{raw[-2:]}"


def sanitize_error_message(msg: str, *, max_len: int = 300) -> str:
    text = str(msg or "")[: max_len * 2]
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1[REDACTED]", text)
    # Strip common credential-looking substrings
    for needle in ("SK", "AC", "Auth Token", "api_key"):
        if needle.lower() in text.lower() and len(text) > 40:
            text = "External provider error"
            break
    return text[:max_len]


def audit(
    action: str,
    *,
    user_id: Optional[str] = None,
    target_id: Optional[str] = None,
    result: str = "ok",
    request_id: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    payload = {
        "ts": utcnow().isoformat(),
        "action": action,
        "user_id": user_id,
        "target_id": target_id,
        "result": result,
        "request_id": request_id,
    }
    if extra:
        # Never allow secrets through extra
        safe = {k: v for k, v in extra.items() if k.lower() not in {"password", "token", "secret", "authorization"}}
        payload["extra"] = safe
    logger.info("audit %s", payload)


def configure_logging() -> None:
    """Back-compat wrapper — prefer app.observability.logging_setup.configure_logging."""
    from app.observability.logging_setup import configure_logging as _cfg

    _cfg()

