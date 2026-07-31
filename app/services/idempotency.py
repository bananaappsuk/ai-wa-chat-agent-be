"""Outbound / inbound idempotency helpers (Redis + Mongo)."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from app.config import settings


def _redis():
    from app.workers.queue import get_redis

    return get_redis()


def make_idempotency_key(*parts: str) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def claim_idempotency(tenant_id: str, key: str) -> bool:
    """Return True if this is the first claim (caller should proceed)."""
    if not key:
        return True
    r = _redis()
    full = f"idem:{tenant_id}:{key}"
    ttl = max(60, int(settings.IDEMPOTENCY_TTL_SECONDS))
    return bool(r.set(full, "1", nx=True, ex=ttl))


def store_idempotency_result(tenant_id: str, key: str, result: dict[str, Any]) -> None:
    if not key:
        return
    r = _redis()
    full = f"idem:result:{tenant_id}:{key}"
    ttl = max(60, int(settings.IDEMPOTENCY_TTL_SECONDS))
    r.set(full, json.dumps(result, default=str), ex=ttl)


def get_idempotency_result(tenant_id: str, key: str) -> Optional[dict[str, Any]]:
    if not key:
        return None
    r = _redis()
    full = f"idem:result:{tenant_id}:{key}"
    raw = r.get(full)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except Exception:
        return None
