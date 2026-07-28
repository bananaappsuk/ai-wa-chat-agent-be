"""Unit tests for Twilio delivery status normalisation / monotonic rules."""
from datetime import datetime, timezone

from app.services.delivery_status import (
    build_status_update,
    normalize_status,
    should_apply_status,
)


def test_normalize_aliases_and_case():
    assert normalize_status("  DELIVERED ") == "delivered"
    assert normalize_status("cancelled") == "canceled"
    assert normalize_status("nope") is None
    assert normalize_status("") is None
    assert normalize_status(None) is None


def test_monotonic_success():
    assert should_apply_status("queued", "sent") is True
    assert should_apply_status("sent", "delivered") is True
    assert should_apply_status("delivered", "read") is True
    assert should_apply_status("delivered", "sent") is False
    assert should_apply_status("read", "delivered") is False
    assert should_apply_status("sent", "sent") is False


def test_failure_overrides_success():
    assert should_apply_status("sent", "failed") is True
    assert should_apply_status("delivered", "undelivered") is True
    assert should_apply_status("failed", "sent") is False
    assert should_apply_status("failed", "failed") is False


def test_build_delivered_sets_timestamps_once():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    update = build_status_update({"status": "sent"}, "delivered", now=now)
    assert update is not None
    assert update["status"] == "delivered"
    assert update["delivered_at"] == now
    assert update["sent_at"] == now

    doc = {"status": "delivered", "delivered_at": now, "sent_at": now}
    later = datetime(2026, 1, 2, tzinfo=timezone.utc)
    update2 = build_status_update(doc, "read", now=later)
    assert update2 is not None
    assert update2["status"] == "read"
    assert "delivered_at" not in update2
    assert update2["read_at"] == later


def test_duplicate_callback_noop():
    assert build_status_update({"status": "delivered"}, "delivered") is None
    assert build_status_update({"status": "delivered"}, "sent") is None


def test_failure_stores_error_fields():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    update = build_status_update(
        {"status": "sent"},
        "failed",
        error_code="30008",
        error_message="Unknown error",
        now=now,
    )
    assert update is not None
    assert update["status"] == "failed"
    assert update["error_code"] == "30008"
    assert update["error"] == "Unknown error"
    assert update["failed_at"] == now


def test_unknown_provider_status_stores_provider_only():
    update = build_status_update({"status": "sent"}, "weird_provider_state")
    assert update is not None
    assert update["provider_status"] == "weird_provider_state"
    assert "status" not in update
