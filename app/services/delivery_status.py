"""Twilio delivery status normalisation and monotonic update helpers."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from app.models.common import utcnow

logger = logging.getLogger(__name__)

CANONICAL_STATUSES = frozenset(
    {
        "queued",
        "accepted",
        "sending",
        "sent",
        "delivered",
        "read",
        "failed",
        "undelivered",
        "canceled",
    }
)

# Successful progression ranks (higher = later).
_SUCCESS_RANK: dict[str, int] = {
    "queued": 10,
    "accepted": 20,
    "sending": 30,
    "sent": 40,
    "delivered": 50,
    "read": 60,
}

_FAILURE_STATUSES = frozenset({"failed", "undelivered", "canceled"})

# Provider aliases → canonical
_ALIASES: dict[str, str] = {
    "cancelled": "canceled",
    "receiving": "accepted",
}


def normalize_status(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip().lower()
    if not value:
        return None
    value = _ALIASES.get(value, value)
    if value in CANONICAL_STATUSES:
        return value
    # Unknown provider value — do not invent a regression target.
    return None


def is_failure_status(status: Optional[str]) -> bool:
    return bool(status) and status in _FAILURE_STATUSES


def should_apply_status(current: Optional[str], incoming: str) -> bool:
    """Return True if ``incoming`` should replace ``current`` (monotonic)."""
    if not incoming:
        return False
    cur = normalize_status(current) if current else None
    if cur is None:
        # Unknown/empty current — apply known incoming.
        return True
    if cur == incoming:
        return False
    if is_failure_status(incoming):
        # Allow terminal failure from any non-identical state.
        return True
    if is_failure_status(cur):
        # Do not overwrite terminal failure with a success status.
        return False
    cur_rank = _SUCCESS_RANK.get(cur, 0)
    new_rank = _SUCCESS_RANK.get(incoming, 0)
    return new_rank > cur_rank


def build_status_update(
    current_doc: dict[str, Any],
    incoming_raw: str,
    *,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """
    Build a Mongo ``$set`` dict for a delivery status update.

    Returns None when the update should be a no-op (duplicate / regressive /
    unknown incoming status that must not change canonical status).
    """
    incoming = normalize_status(incoming_raw)
    if incoming is None:
        # Preserve unknown provider status without regressing canonical status.
        provider = (incoming_raw or "").strip().lower() or None
        if not provider:
            return None
        existing_provider = (current_doc.get("provider_status") or "").strip().lower()
        if existing_provider == provider:
            return None
        ts = now or utcnow()
        return {
            "provider_status": provider,
            "status_updated_at": ts,
        }

    current = current_doc.get("status")
    if not should_apply_status(current, incoming):
        return None

    ts = now or utcnow()
    update: dict[str, Any] = {
        "status": incoming,
        "status_updated_at": ts,
        "updated_at": ts,
        "provider_status": incoming,
    }

    if is_failure_status(incoming):
        if error_code is not None and str(error_code).strip():
            update["error_code"] = str(error_code).strip()
        if error_message is not None and str(error_message).strip():
            msg = str(error_message).strip()[:500]
            update["error_message"] = msg
            update["error"] = msg
        if not current_doc.get("failed_at"):
            update["failed_at"] = ts
    else:
        # Clear stale error fields when moving to a success status after a prior fail
        # is blocked by monotonic rules, so this branch is normally clean.
        rank = _SUCCESS_RANK.get(incoming, 0)
        if rank >= _SUCCESS_RANK["sent"] and not current_doc.get("sent_at"):
            update["sent_at"] = ts
        if rank >= _SUCCESS_RANK["delivered"] and not current_doc.get("delivered_at"):
            update["delivered_at"] = ts
        if incoming == "read" and not current_doc.get("read_at"):
            update["read_at"] = ts

    return update


def log_duplicate_sid(collection: str, sid: str, count: int) -> None:
    if count > 1:
        logger.warning(
            "Multiple %s documents share twilio_sid suffix=...%s count=%s",
            collection,
            sid[-6:] if sid and len(sid) > 6 else "?",
            count,
        )
