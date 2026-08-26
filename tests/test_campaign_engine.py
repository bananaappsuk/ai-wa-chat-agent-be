"""Tests for campaign sending engine + analytics (E1/E2)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.services.campaign_service import (
    compute_rates,
    is_retryable_error,
    parse_scheduled_at,
    progress_percentage,
    recipient_should_apply,
    recount_campaign_fields,
)
from app.services.whatsapp_window import WINDOW_CLOSED_ERROR


def test_rates_handle_zero_denominators():
    rates = compute_rates({"total_recipients": 0, "sent_count": 0, "delivered_count": 0, "read_count": 0, "failed_count": 0, "replied_count": 0})
    assert rates["delivery_rate"] == 0.0
    assert rates["read_rate"] == 0.0
    assert rates["failure_rate"] == 0.0
    assert rates["reply_rate"] == 0.0


def test_rates_and_progress():
    camp = {
        "total_recipients": 10,
        "sent_count": 8,
        "delivered_count": 5,
        "read_count": 2,
        "failed_count": 2,
        "skipped_count": 0,
        "cancelled_count": 0,
        "replied_count": 1,
    }
    rates = compute_rates(camp)
    assert rates["delivery_rate"] > 0
    assert progress_percentage(camp) > 0
    fields = recount_campaign_fields(
        {"sent": 3, "delivered": 2, "read": 1, "failed": 2, "skipped": 1, "queued": 1},
        10,
    )
    assert fields["total_recipients"] == 10
    assert fields["failed_count"] == 2


def test_status_cannot_move_backwards():
    assert recipient_should_apply("read", "delivered") is False
    assert recipient_should_apply("delivered", "sent") is False
    assert recipient_should_apply("sent", "delivered") is True
    assert recipient_should_apply("delivered", "read") is True


def test_retryable_classification():
    assert is_retryable_error("connection timeout") is True
    assert is_retryable_error("HTTP 503") is True
    assert is_retryable_error(WINDOW_CLOSED_ERROR) is False
    assert is_retryable_error("blacklisted") is False
    assert is_retryable_error("Template is not approved") is False
    assert is_retryable_error("skipped_closed_window") is False
    assert is_retryable_error("Campaign limited to open WhatsApp windows — recipient skipped") is False


def test_skipped_closed_window_analytics_counts():
    from app.services.campaign_service import finalize_status_from_counts, recount_ai_generation_fields

    fields = recount_campaign_fields(
        {"read": 1, "skipped": 1},
        2,
    )
    assert fields["skipped_count"] == 1
    assert fields["failed_count"] == 0
    assert fields["sent_count"] == 1

    rates = compute_rates(
        {
            "total_recipients": 2,
            "sent_count": 1,
            "delivered_count": 0,
            "read_count": 1,
            "failed_count": 0,
            "skipped_count": 1,
            "cancelled_count": 0,
            "replied_count": 0,
        }
    )
    assert rates["failure_rate"] == 0.0

    final = finalize_status_from_counts(
        {
            "total_recipients": 2,
            "sent_count": 1,
            "failed_count": 0,
            "skipped_count": 1,
            "cancelled_count": 0,
        }
    )
    assert final == "partially_completed"

    ai = recount_ai_generation_fields({"ready": 1, "skipped": 1, "failed": 0})
    assert ai["ai_failed_count"] == 0
    assert ai["ai_ready_count"] == 1


def test_scheduled_at_utc_aware():
    dt = parse_scheduled_at("2026-08-01T12:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset() == timedelta(0)


@pytest.mark.asyncio
async def test_create_rejects_no_recipients():
    from app.routes import campaigns as camp_routes
    from app.models.campaign import CampaignCreate

    with pytest.raises(HTTPException) as exc:
        await camp_routes.create_campaign(
            CampaignCreate(name="x", message="hi", lead_ids=[], recipients=[]),
            user={"_id": ObjectId(), "plan": "business", "subscription_status": "active"},
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_campaign_with_recipients_and_dedupe():
    from app.routes import campaigns as camp_routes
    from app.models.campaign import CampaignCreate

    user_id = ObjectId()
    camp_oid = ObjectId()
    db = MagicMock()
    db.campaigns.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=camp_oid))
    db.campaigns.delete_one = AsyncMock()
    db.campaigns.update_one = AsyncMock()
    db.campaigns.find_one = AsyncMock(
        return_value={
            "_id": camp_oid,
            "user_id": str(user_id),
            "name": "Promo",
            "status": "draft",
            "total_recipients": 1,
            "created_at": datetime.now(timezone.utc),
        }
    )
    class _EmptyAsync:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    db.blacklist.find = MagicMock(return_value=_EmptyAsync())
    db.leads.find_one = AsyncMock(
        return_value={
            "_id": ObjectId(),
            "phone": "+447700900000",
            "name": "Lead",
            "whatsapp_consent_status": "opted_in",
            "blacklisted": False,
        }
    )
    db.campaign_recipients.insert_many = AsyncMock()
    db.campaign_recipients.aggregate = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[{"_id": "pending", "n": 1}]))
    )

    with (
        patch.object(camp_routes, "get_db", return_value=db),
        patch.object(camp_routes.ws_manager, "push", new=AsyncMock()),
    ):
        result = await camp_routes.create_campaign(
            CampaignCreate(
                name="Promo",
                message="Hello",
                recipients=["+447700900000", "+447700900000", "not-a-phone"],
            ),
            user={"_id": user_id, "plan": "business", "subscription_status": "active"},
        )
    assert result["name"] == "Promo"
    assert db.campaign_recipients.insert_many.await_count == 1
    rows = db.campaign_recipients.insert_many.await_args.args[0]
    phones = [r["phone"] for r in rows if r["status"] == "pending"]
    assert phones.count("+447700900000") == 1
    assert len(phones) >= 1


@pytest.mark.asyncio
async def test_cross_tenant_campaign_access_denied():
    from app.routes import campaigns as camp_routes

    db = MagicMock()
    db.campaigns.find_one = AsyncMock(return_value=None)
    with patch.object(camp_routes, "get_db", return_value=db):
        with pytest.raises(HTTPException) as exc:
            await camp_routes.get_campaign(str(ObjectId()), user={"_id": ObjectId(), "plan": "business", "subscription_status": "active"})
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_start_only_draft_or_scheduled():
    from app.routes import campaigns as camp_routes

    user_id = ObjectId()
    cid = ObjectId()
    db = MagicMock()
    db.campaigns.find_one = AsyncMock(
        return_value={"_id": cid, "user_id": str(user_id), "status": "running", "total_recipients": 5}
    )
    with patch.object(camp_routes, "get_db", return_value=db):
        with pytest.raises(HTTPException) as exc:
            await camp_routes.start_campaign(str(cid), user={"_id": user_id, "plan": "business", "subscription_status": "active"}, confirm_marketing=True)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_pause_resume_cancel_guards():
    from app.routes import campaigns as camp_routes

    user_id = ObjectId()
    cid = ObjectId()

    async def owned(status: str):
        return {"_id": cid, "user_id": str(user_id), "status": status, "total_recipients": 2}

    with patch.object(camp_routes, "_get_owned", new=AsyncMock(return_value=await owned("draft"))):
        with pytest.raises(HTTPException):
            await camp_routes.pause_campaign(str(cid), user={"_id": user_id, "plan": "business", "subscription_status": "active"})

    with patch.object(camp_routes, "_get_owned", new=AsyncMock(return_value=await owned("running"))):
        with pytest.raises(HTTPException):
            await camp_routes.resume_campaign(str(cid), user={"_id": user_id, "plan": "business", "subscription_status": "active"})


def test_send_recipient_idempotent_claim():
    from app.workers import campaign_tasks as ct

    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = str(ObjectId())
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={"_id": ObjectId(campaign_id), "user_id": user_id, "status": "running", "message": "hi"}
    )
    # First claim returns None → already processed
    db.campaign_recipients.find_one_and_update = MagicMock(return_value=None)

    with patch.object(ct, "_db", return_value=db), patch.object(ct.twilio_service, "send_whatsapp") as send:
        ct.send_campaign_recipient(user_id, campaign_id, recipient_id)
    send.assert_not_called()


def test_window_closed_skips_without_template():
    from app.workers import campaign_tasks as ct

    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = ObjectId()
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={
            "_id": ObjectId(campaign_id),
            "user_id": user_id,
            "status": "running",
            "message": "hi",
            "template_id": None,
            "content_sid": None,
        }
    )
    recipient = {
        "_id": recipient_id,
        "phone": "+447700900099",
        "status": "processing",
        "attempt_count": 1,
        "lead_id": None,
    }
    db.campaign_recipients.find_one_and_update = MagicMock(side_effect=[recipient, {**recipient, "status": "skipped"}])
    db.blacklist.find_one = MagicMock(return_value=None)
    db.leads.find_one = MagicMock(
        return_value={
            "phone": "+447700900099",
            "whatsapp_consent_status": "opted_in",
            "blacklisted": False,
            "whatsapp_window_expires_at": None,
            "last_inbound_at": None,
        }
    )  # opted in but window closed
    db.campaign_recipients.aggregate = MagicMock(return_value=[])
    db.campaigns.update_one = MagicMock()
    db.campaign_recipients.count_documents = MagicMock(return_value=0)

    with (
        patch.object(ct, "_db", return_value=db),
        patch.object(ct, "_publish"),
        patch.object(ct, "_refresh_campaign_counters", return_value={}),
        patch.object(ct, "_maybe_complete_campaign"),
        patch("app.services.idempotency.claim_idempotency", return_value=True),
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch.object(ct.twilio_service, "send_whatsapp") as send,
    ):
        ct.send_campaign_recipient(user_id, campaign_id, str(recipient_id))
    send.assert_not_called()
    final_set = db.campaign_recipients.find_one_and_update.call_args_list[-1][0][1]["$set"]
    assert final_set["status"] == "skipped"


def test_approved_template_sends_outside_window():
    from app.workers import campaign_tasks as ct

    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = ObjectId()
    tmpl_id = ObjectId()
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={
            "_id": ObjectId(campaign_id),
            "user_id": user_id,
            "status": "running",
            "template_id": str(tmpl_id),
            "content_sid": "HXabc",
            "content_variables": {"1": "A"},
        }
    )
    recipient = {
        "_id": recipient_id,
        "phone": "+447700900088",
        "status": "processing",
        "attempt_count": 1,
    }
    db.campaign_recipients.find_one_and_update = MagicMock(
        side_effect=[recipient, {**recipient, "status": "sent", "twilio_sid": "SMx"}]
    )
    db.blacklist.find_one = MagicMock(return_value=None)
    db.leads.find_one = MagicMock(
        return_value={
            "phone": "+447700900088",
            "whatsapp_consent_status": "opted_in",
            "blacklisted": False,
        }
    )
    db.templates.find_one = MagicMock(
        return_value={"_id": tmpl_id, "user_id": user_id, "status": "approved", "content_sid": "HXabc"}
    )
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=ObjectId()))

    with (
        patch.object(ct, "_db", return_value=db),
        patch.object(ct, "_publish"),
        patch.object(ct, "_refresh_campaign_counters", return_value={}),
        patch.object(ct, "_maybe_complete_campaign"),
        patch("app.services.idempotency.claim_idempotency", return_value=True),
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch.object(
            ct.twilio_service,
            "assert_whatsapp_template_approved_for_out_of_session",
            return_value={"whatsapp_status": "approved", "body": "Hi", "friendly_name": "t"},
        ),
        patch.object(ct.twilio_service, "log_template_send_gate"),
        patch.object(ct.twilio_service, "send_whatsapp", return_value={"sid": "SMx", "status": "queued"}) as send,
    ):
        ct.send_campaign_recipient(user_id, campaign_id, str(recipient_id))
    send.assert_called_once()
    assert send.call_args.kwargs.get("content_sid") == "HXabc"


def test_under_review_template_skips_closed_window_without_send():
    from app.services.whatsapp_template_approval import (
        TEMPLATE_UNDER_REVIEW,
        WhatsAppTemplateNotApprovedError,
    )
    from app.workers import campaign_tasks as ct

    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = ObjectId()
    tmpl_id = ObjectId()
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={
            "_id": ObjectId(campaign_id),
            "user_id": user_id,
            "status": "running",
            "template_id": str(tmpl_id),
            "content_sid": "HXunder",
            "content_variables": {},
        }
    )
    recipient = {
        "_id": recipient_id,
        "phone": "+447700900077",
        "status": "processing",
        "attempt_count": 1,
    }
    db.campaign_recipients.find_one_and_update = MagicMock(
        side_effect=[recipient, {**recipient, "status": "skipped"}]
    )
    db.blacklist.find_one = MagicMock(return_value=None)
    db.leads.find_one = MagicMock(
        return_value={
            "phone": "+447700900077",
            "whatsapp_consent_status": "opted_in",
            "blacklisted": False,
        }
    )
    db.templates.find_one = MagicMock(
        return_value={
            "_id": tmpl_id,
            "user_id": user_id,
            "status": "approved",
            "content_sid": "HXunder",
            "name": "aisummercamp26",
        }
    )

    with (
        patch.object(ct, "_db", return_value=db),
        patch.object(ct, "_publish"),
        patch.object(ct, "_refresh_campaign_counters", return_value={}),
        patch.object(ct, "_maybe_complete_campaign"),
        patch("app.services.idempotency.claim_idempotency", return_value=True),
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch.object(
            ct.twilio_service,
            "assert_whatsapp_template_approved_for_out_of_session",
            side_effect=WhatsAppTemplateNotApprovedError(
                whatsapp_status="under_review",
                content_sid="HXunder",
                template_name="aisummercamp26",
            ),
        ),
        patch.object(ct.twilio_service, "log_template_send_gate"),
        patch.object(ct.twilio_service, "send_whatsapp") as send,
    ):
        ct.send_campaign_recipient(user_id, campaign_id, str(recipient_id))
    send.assert_not_called()
    final_set = db.campaign_recipients.find_one_and_update.call_args_list[-1][0][1]["$set"]
    assert final_set["status"] == "skipped"
    assert final_set["error_code"] == TEMPLATE_UNDER_REVIEW
    assert "Under Review" in final_set["error_message"]
    assert "aisummercamp26" in final_set["error_message"]


def test_pause_prevents_send():
    from app.workers import campaign_tasks as ct

    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={"_id": ObjectId(campaign_id), "user_id": user_id, "status": "paused"}
    )
    with patch.object(ct, "_db", return_value=db), patch.object(ct.twilio_service, "send_whatsapp") as send:
        ct.send_campaign_recipient(user_id, campaign_id, str(ObjectId()))
    send.assert_not_called()


def test_process_due_scheduled_campaigns():
    from app.workers import campaign_tasks as ct

    cid = ObjectId()
    uid = ObjectId()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    db = MagicMock()
    db.campaigns.find = MagicMock(
        return_value=MagicMock(
            limit=MagicMock(
                return_value=[
                    {"_id": cid, "user_id": str(uid), "status": "scheduled", "scheduled_at": past}
                ]
            )
        )
    )
    db.campaigns.update_one = MagicMock(return_value=SimpleNamespace(modified_count=1))
    q = MagicMock()
    with patch.object(ct, "_db", return_value=db), patch.object(ct, "_queue", return_value=q):
        n = ct.process_due_scheduled_campaigns()
    assert n == 1
    q.enqueue.assert_called_once()
