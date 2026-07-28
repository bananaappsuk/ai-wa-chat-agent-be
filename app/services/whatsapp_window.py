"""WhatsApp 24-hour customer service window helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

WINDOW_HOURS = 24
WINDOW_CLOSED_ERROR = (
    "WhatsApp customer service window closed. Use an approved template."
)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_window_expiry(last_inbound_at: datetime) -> datetime:
    last = _as_utc(last_inbound_at)
    assert last is not None
    return last + timedelta(hours=WINDOW_HOURS)


def is_whatsapp_window_open(lead: Optional[dict], *, now: Optional[datetime] = None) -> bool:
    """True when free-form messages are allowed for this lead."""
    if not lead:
        return False
    expires = _as_utc(lead.get("whatsapp_window_expires_at"))
    if expires is None:
        # Fall back to last_inbound_at + 24h if expiry not stored yet.
        last = _as_utc(lead.get("last_inbound_at"))
        if last is None:
            return False
        expires = compute_window_expiry(last)
    now = _as_utc(now) or datetime.now(timezone.utc)
    return now < expires


def get_whatsapp_window_status(
    lead: Optional[dict], *, now: Optional[datetime] = None
) -> dict[str, Any]:
    now = _as_utc(now) or datetime.now(timezone.utc)
    last = _as_utc((lead or {}).get("last_inbound_at")) if lead else None
    expires = _as_utc((lead or {}).get("whatsapp_window_expires_at")) if lead else None
    if expires is None and last is not None:
        expires = compute_window_expiry(last)
    open_ = bool(expires and now < expires)
    return {
        "open": open_,
        "last_inbound_at": last.isoformat() if last else None,
        "whatsapp_window_expires_at": expires.isoformat() if expires else None,
        "seconds_remaining": int((expires - now).total_seconds()) if open_ and expires else 0,
    }


def inbound_window_fields(now: Optional[datetime] = None) -> dict[str, datetime]:
    """Fields to $set on a lead after an inbound WhatsApp message."""
    ts = _as_utc(now) or datetime.now(timezone.utc)
    return {
        "last_inbound_at": ts,
        "whatsapp_window_expires_at": compute_window_expiry(ts),
    }
