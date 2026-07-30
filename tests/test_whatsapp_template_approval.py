"""Tests for WhatsApp Content Template Meta approval gating."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.twilio_errors import classify_send_error, is_retryable_category
from app.services.whatsapp_template_approval import (
    TEMPLATE_PAUSED,
    TEMPLATE_REJECTED,
    TEMPLATE_UNDER_REVIEW,
    WhatsAppTemplateNotApprovedError,
    display_status_label,
    error_code_for_whatsapp_status,
    is_whatsapp_template_sendable,
    mask_content_sid,
    normalize_whatsapp_approval_status,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("approved", "approved"),
        ("Approved", "approved"),
        ("under review", "under_review"),
        ("Under Review", "under_review"),
        ("in-review", "under_review"),
        ("pending", "under_review"),
        ("unsubmitted", "unsubmitted"),
        ("rejected", "rejected"),
        ("paused", "paused"),
        ("", "unknown"),
        (None, "unknown"),
    ],
)
def test_normalize_whatsapp_approval_status(raw, expected):
    assert normalize_whatsapp_approval_status(raw) == expected


@pytest.mark.parametrize(
    "status,code",
    [
        ("under_review", TEMPLATE_UNDER_REVIEW),
        ("pending", TEMPLATE_UNDER_REVIEW),  # Twilio "pending" == Under Review
        ("rejected", TEMPLATE_REJECTED),
        ("paused", TEMPLATE_PAUSED),
    ],
)
def test_error_code_for_blocked_status(status, code):
    assert error_code_for_whatsapp_status(status) == code
    assert not is_whatsapp_template_sendable(status)


def test_approved_is_sendable():
    assert is_whatsapp_template_sendable("approved")


def test_user_facing_under_review_message():
    err = WhatsAppTemplateNotApprovedError(
        whatsapp_status="under_review",
        content_sid="HXca3f75be10673163985ebf25cc783484",
        template_name="aisummercamp26",
    )
    assert err.error_code == TEMPLATE_UNDER_REVIEW
    msg = err.user_message()
    assert "aisummercamp26" in msg
    assert "Under Review" in msg
    assert "Business-initiated" in msg
    assert classify_send_error(err) == "template_error"
    assert not is_retryable_category(classify_send_error(err))


def test_classify_does_not_treat_meta_message_as_window_closed():
    text = (
        "Template 'aisummercamp26' is currently Under Review by Meta.\n\n"
        "Business-initiated WhatsApp messages cannot be sent until the template is approved."
    )
    assert classify_send_error(text) == "template_error"


def test_mask_content_sid():
    sid = "HXca3f75be10673163985ebf25cc783484"
    masked = mask_content_sid(sid)
    assert masked.startswith("HXca")
    assert masked != sid


def test_display_status_label():
    assert display_status_label("under_review") == "Under Review"
    assert display_status_label("approved") == "Approved"


def test_assert_approved_passes():
    from app.services import twilio_service

    info = {
        "content_sid": "HXapproved",
        "whatsapp_status": "approved",
        "friendly_name": "welcome",
        "body": "Hi",
    }
    with patch.object(twilio_service, "get_content_template_info", return_value=info):
        out = twilio_service.assert_whatsapp_template_approved_for_out_of_session("HXapproved")
        assert out["whatsapp_status"] == "approved"


@pytest.mark.parametrize(
    "status,code",
    [
        ("under_review", TEMPLATE_UNDER_REVIEW),
        ("pending", TEMPLATE_UNDER_REVIEW),
        ("rejected", TEMPLATE_REJECTED),
        ("paused", TEMPLATE_PAUSED),
    ],
)
def test_assert_blocked_statuses(status, code):
    from app.services import twilio_service

    info = {
        "content_sid": "HXblocked",
        "whatsapp_status": normalize_whatsapp_approval_status(status),
        "friendly_name": "aisummercamp26",
        "body": "Hi",
    }
    with patch.object(twilio_service, "get_content_template_info", return_value=info):
        with pytest.raises(WhatsAppTemplateNotApprovedError) as ei:
            twilio_service.assert_whatsapp_template_approved_for_out_of_session(
                "HXblocked", template_name="aisummercamp26"
            )
        assert ei.value.error_code == code


def test_closed_window_under_review_reason_code():
    err = WhatsAppTemplateNotApprovedError(
        whatsapp_status="under_review",
        content_sid="HX1",
        template_name="aisummercamp26",
    )
    assert classify_send_error(err) == "template_error"
    assert err.error_code == TEMPLATE_UNDER_REVIEW


def test_get_content_template_info_normalizes_status():
    from app.services import twilio_service

    content = MagicMock()
    content.types = {"twilio/text": {"body": "Hello {{1}}"}}
    content.friendly_name = "aisummercamp26"
    content.variables = {"1": "name"}
    content.language = "en_GB"

    approvals = MagicMock()
    approvals.whatsapp = {"status": "pending", "category": "MARKETING"}

    client = MagicMock()
    client.content.v1.contents.return_value.fetch.return_value = content
    client.content.v1.contents.return_value.approval_fetch.return_value.fetch.return_value = approvals

    with patch.object(twilio_service, "_client", return_value=client), patch(
        "app.services.twilio_service.settings"
    ) as settings:
        settings.TWILIO_ACCOUNT_SID = "ACxxx"
        settings.TWILIO_AUTH_TOKEN = "token"
        info = twilio_service.get_content_template_info("HXca3f75be10673163985ebf25cc783484")
    assert info["whatsapp_status"] == "under_review"
    assert info["friendly_name"] == "aisummercamp26"
    assert info["whatsapp_category"] == "MARKETING"
    assert info["provider"] == "twilio_content"
