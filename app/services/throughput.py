"""Redis-backed WhatsApp throughput / backpressure controls."""
from __future__ import annotations

import time
from typing import Optional

from fastapi import HTTPException

from app.config import settings


def _redis():
    from app.workers.queue import get_redis

    return get_redis()


def acquire_send_permit(*, user_id: str, priority: str = "default") -> bool:
    """
    Try to acquire a global + tenant send permit.
    Returns False when deferred (worker should requeue with delay).
    Live-chat / high priority still counts toward limits but is never rejected at API for soft limit.
    """
    r = _redis()
    now = int(time.time())
    # Per-second global
    sec_key = f"wa:tps:{now}"
    per_sec = max(1, int(settings.WHATSAPP_MESSAGES_PER_SECOND))
    count = int(r.incr(sec_key))
    if count == 1:
        r.expire(sec_key, 2)
    if count > per_sec:
        return False

    # Per-minute global
    minute = now // 60
    min_key = f"wa:tpm:{minute}"
    per_min = max(1, int(settings.WHATSAPP_MESSAGES_PER_MINUTE))
    mcount = int(r.incr(min_key))
    if mcount == 1:
        r.expire(min_key, 70)
    if mcount > per_min:
        return False

    # Tenant per-minute
    t_key = f"wa:tpm:{user_id}:{minute}"
    t_limit = max(1, int(settings.WHATSAPP_TENANT_MESSAGES_PER_MINUTE))
    tcount = int(r.incr(t_key))
    if tcount == 1:
        r.expire(t_key, 70)
    if tcount > t_limit:
        return False

    # Concurrent slots
    conc_key = "wa:concurrent"
    max_c = max(1, int(settings.WHATSAPP_MAX_CONCURRENT_SENDS))
    current = int(r.incr(conc_key))
    if current == 1:
        r.expire(conc_key, 120)
    if current > max_c:
        r.decr(conc_key)
        return False
    return True


def release_send_permit() -> None:
    try:
        r = _redis()
        r.decr("wa:concurrent")
    except Exception:
        pass


def queue_depth(queue_name: Optional[str] = None) -> int:
    from app.workers.queue import get_queue

    try:
        return len(get_queue(queue_name))
    except Exception:
        return 0


def assert_bulk_enqueue_allowed(*, estimated_jobs: int = 1) -> None:
    """Block oversized campaign/blast enqueue when queue is too deep."""
    depth = queue_depth(settings.RQ_BULK_QUEUE_NAME)
    max_depth = max(10, int(settings.WHATSAPP_QUEUE_MAX_DEPTH))
    if depth + estimated_jobs > max_depth:
        raise HTTPException(
            status_code=429,
            detail="Outbound queue is full. Try again later or reduce campaign size.",
        )
