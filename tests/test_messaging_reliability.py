"""B4 + B7–B15 messaging reliability unit tests."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.whatsapp_consent import (
    is_optout_keyword,
    is_optin_keyword,
    normalize_keyword_body,
    consent_update_fields,
)
from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility
from app.services.twilio_errors import classify_send_error, is_retryable_category
from app.services.retry_backoff import compute_retry_delay_seconds, max_retries
from app.services.whatsapp_status import whatsapp_status_payload, detect_sender_type
from app.services.idempotency import make_idempotency_key
from app.config import settings


def test_optout_keyword_case_insensitive_and_punctuation():
    assert is_optout_keyword("STOP")
    assert is_optout_keyword(" stop! ")
    assert is_optout_keyword("Unsubscribe")
    assert not is_optout_keyword("please stop messaging me later")
    assert normalize_keyword_body('"STOP"') == "stop"


def test_optin_keywords():
    assert is_optin_keyword("START")
    assert is_optin_keyword("yes")
    assert is_optin_keyword("UNSTOP")


def test_consent_update_opt_out_sets_blacklist_flags():
    fields = consent_update_fields(status="opted_out", source="keyword_optout", reason="STOP")
    assert fields["whatsapp_consent_status"] == "opted_out"
    assert fields["blacklisted"] is True
    assert fields["ai_paused"] is True
    assert fields["whatsapp_opted_out_at"] is not None


def test_consent_update_opt_in_clears_opt_out():
    fields = consent_update_fields(status="opted_in", source="manual")
    assert fields["whatsapp_consent_status"] == "opted_in"
    assert fields["blacklisted"] is False
    assert fields["whatsapp_opted_out_at"] is None


def test_marketing_requires_opt_in():
    lead = {"phone": "+447700900001", "whatsapp_consent_status": "unknown", "blacklisted": False}
    r = get_whatsapp_send_eligibility(lead=lead, purpose="marketing", has_template=True)
    assert r.allowed is False
    assert r.reason_code == "consent_required"


def test_opted_in_marketing_allowed_with_template():
    lead = {
        "phone": "+447700900001",
        "whatsapp_consent_status": "opted_in",
        "blacklisted": False,
    }
    with patch("app.services.whatsapp_eligibility._sender_ok", return_value=True):
        r = get_whatsapp_send_eligibility(lead=lead, purpose="marketing", has_template=True)
    assert r.allowed is True


def test_opted_out_blocks_even_with_template():
    lead = {
        "phone": "+447700900001",
        "whatsapp_consent_status": "opted_out",
        "blacklisted": True,
    }
    with patch("app.services.whatsapp_eligibility._sender_ok", return_value=True):
        r = get_whatsapp_send_eligibility(lead=lead, purpose="conversational", has_template=True)
    assert r.allowed is False
    assert r.reason_code == "consent_blocked"


def test_open_window_conversational_allowed():
    from datetime import datetime, timedelta, timezone

    lead = {
        "phone": "+447700900001",
        "whatsapp_consent_status": "unknown",
        "blacklisted": False,
        "whatsapp_window_expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    with patch("app.services.whatsapp_eligibility._sender_ok", return_value=True):
        r = get_whatsapp_send_eligibility(lead=lead, purpose="conversational", has_template=False)
    assert r.allowed is True


def test_closed_window_conversational_blocked():
    from datetime import datetime, timedelta, timezone

    lead = {
        "phone": "+447700900001",
        "whatsapp_consent_status": "unknown",
        "blacklisted": False,
        "whatsapp_window_expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }
    with patch("app.services.whatsapp_eligibility._sender_ok", return_value=True):
        r = get_whatsapp_send_eligibility(lead=lead, purpose="conversational", has_template=False)
    assert r.allowed is False
    assert r.reason_code == "window_closed"


def test_template_does_not_bypass_missing_marketing_consent():
    lead = {"phone": "+447700900001", "whatsapp_consent_status": "pending", "blacklisted": False}
    with patch("app.services.whatsapp_eligibility._sender_ok", return_value=True):
        r = get_whatsapp_send_eligibility(lead=lead, purpose="campaign", has_template=True)
    assert r.allowed is False
    assert r.reason_code == "consent_required"


def test_error_classification_retryable_and_not():
    assert classify_send_error("timeout connecting") == "retryable"
    assert is_retryable_category(classify_send_error("timeout"))
    assert classify_send_error("blacklisted") == "consent_blocked"
    assert not is_retryable_category(classify_send_error("opted out"))
    assert classify_send_error("HTTP 429 rate limit") == "provider_rate_limited"
    # 63016 often means Content Template was not Meta-approved (see twilio_errors._CODE_MAP).
    assert classify_send_error("Error 63016") == "template_error"
    assert not is_retryable_category(classify_send_error("Error 63016"))


def test_retry_backoff_within_limits():
    for attempt in range(1, 8):
        d = compute_retry_delay_seconds(attempt)
        assert 1 <= d <= max(int(settings.WHATSAPP_RETRY_MAX_SECONDS), 1)
    assert max_retries() >= 0


def test_whatsapp_status_exposes_no_secrets():
    payload = whatsapp_status_payload()
    blob = str(payload).lower()
    assert "auth_token" not in blob
    assert "twilio_auth" not in blob
    assert "sender_type" in payload
    assert "warnings" in payload
    assert detect_sender_type() in ("sandbox", "direct", "messaging_service", "none")


def test_idempotency_key_deterministic():
    a = make_idempotency_key("outbound", "tenant1", "msg1")
    b = make_idempotency_key("outbound", "tenant1", "msg1")
    c = make_idempotency_key("outbound", "tenant2", "msg1")
    assert a == b
    assert a != c


def test_claim_idempotency_tenant_scoped():
    r = MagicMock()
    r.set.return_value = True
    with patch("app.services.idempotency._redis", return_value=r):
        from app.services.idempotency import claim_idempotency

        assert claim_idempotency("tenantA", "k1") is True
        args = r.set.call_args[0]
        assert args[0].startswith("idem:tenantA:")


@pytest.mark.asyncio
async def test_whatsapp_status_endpoint_no_secrets():
    from app.routes import settings as settings_route

    out = await settings_route.whatsapp_status(_user={"_id": "u1"})
    assert "TWILIO_AUTH_TOKEN" not in str(out)
    assert "sender_configured" in out


def test_opt_out_confirmation_purpose_allowed_when_opted_out():
    lead = {
        "phone": "+447700900001",
        "whatsapp_consent_status": "opted_out",
        "blacklisted": True,
    }
    with patch("app.services.whatsapp_eligibility._sender_ok", return_value=True):
        r = get_whatsapp_send_eligibility(lead=lead, purpose="opt_out_confirmation")
    assert r.allowed is True


def test_reconciliation_marks_stale():
    from app.services import reconciliation
    from datetime import datetime, timedelta, timezone

    class FakeColl:
        def __init__(self):
            self.updates = []

        def find(self, *a, **k):
            return self

        def sort(self, *a, **k):
            return self

        def limit(self, n):
            return [
                {
                    "_id": "m1",
                    "status": "queued",
                    "created_at": datetime.now(timezone.utc) - timedelta(hours=5),
                    "twilio_sid": None,
                }
            ]

        def update_one(self, filt, update):
            self.updates.append((filt, update))
            return MagicMock(modified_count=1)

    class FakeDB:
        def __init__(self):
            self.messages = FakeColl()
            self.campaign_recipients = FakeColl()

    class FakeClient:
        def __getitem__(self, name):
            return FakeDB()

    with patch("pymongo.MongoClient", return_value=FakeClient()):
        result = reconciliation.reconcile_stale_messages(batch_size=10)
    assert result["inspected"] >= 1
    assert result["repaired"] >= 1
