"""Retry delay helpers for WhatsApp outbound sends."""
from __future__ import annotations

import random

from app.config import settings


def compute_retry_delay_seconds(attempt: int) -> int:
    """Exponential backoff with jitter. attempt is 1-based."""
    base = max(1, int(settings.WHATSAPP_RETRY_BASE_SECONDS))
    max_s = max(base, int(settings.WHATSAPP_RETRY_MAX_SECONDS))
    jitter = max(0, int(settings.WHATSAPP_RETRY_JITTER_SECONDS))
    exp = min(max_s, base * (2 ** max(0, attempt - 1)))
    delay = exp + (random.randint(0, jitter) if jitter else 0)
    return min(max_s, max(1, delay))


def max_retries() -> int:
    return max(0, int(settings.WHATSAPP_MAX_RETRIES))
