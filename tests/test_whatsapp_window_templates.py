"""Tests for WhatsApp 24h customer service window + templates (B2/B3)."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.services.whatsapp_window import (
    WINDOW_CLOSED_ERROR,
    compute_window_expiry,
    get_whatsapp_window_status,
    inbound_window_fields,
    is_whatsapp_window_open,
)


def test_inbound_updates_last_inbound_and_expiry():
    now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    fields = inbound_window_fields(now)
    assert fields["last_inbound_at"] == now
    assert fields["whatsapp_window_expires_at"] == now + timedelta(hours=24)
    assert fields["whatsapp_window_expires_at"].tzinfo is not None


def test_free_form_allowed_inside_24_hours():
    now = datetime.now(timezone.utc)
    lead = {
        "last_inbound_at": now - timedelta(hours=1),
        "whatsapp_window_expires_at": now + timedelta(hours=23),
    }
    assert is_whatsapp_window_open(lead, now=now) is True
    status = get_whatsapp_window_status(lead, now=now)
    assert status["open"] is True
    assert status["seconds_remaining"] > 0


def test_free_form_blocked_outside_24_hours():
    now = datetime.now(timezone.utc)
    lead = {
        "last_inbound_at": now - timedelta(hours=25),
        "whatsapp_window_expires_at": now - timedelta(hours=1),
    }
    assert is_whatsapp_window_open(lead, now=now) is False
    assert get_whatsapp_window_status(lead, now=now)["open"] is False


def test_no_last_inbound_means_closed():
    assert is_whatsapp_window_open(None) is False
    assert is_whatsapp_window_open({}) is False
    assert is_whatsapp_window_open({"last_inbound_at": None}) is False
    status = get_whatsapp_window_status({"last_inbound_at": None})
    assert status["open"] is False
    assert status["whatsapp_window_expires_at"] is None


def test_expiry_fallback_from_last_inbound_only():
    now = datetime.now(timezone.utc)
    lead = {"last_inbound_at": now - timedelta(hours=2)}
    assert is_whatsapp_window_open(lead, now=now) is True
    assert compute_window_expiry(lead["last_inbound_at"]) == lead["last_inbound_at"] + timedelta(hours=24)


@pytest.mark.asyncio
async def test_send_message_blocks_free_form_outside_window():
    from app.routes import messages as messages_route
    from app.models.message import MessageSend

    lead = {
        "_id": ObjectId(),
        "phone": "+447700900000",
        "blacklisted": False,
        "last_inbound_at": datetime.now(timezone.utc) - timedelta(hours=30),
        "whatsapp_window_expires_at": datetime.now(timezone.utc) - timedelta(hours=6),
    }
    user = {"_id": ObjectId()}

    with patch.object(messages_route.lead_service, "get_lead", new=AsyncMock(return_value=lead)):
        with patch("app.security.rate_limit.rate_limit_send"):
            with patch("app.services.whatsapp_eligibility._sender_ok", return_value=True):
                with pytest.raises(HTTPException) as exc:
                    await messages_route.send_message(
                        MessageSend(lead_id=str(lead["_id"]), message="hello"),
                        user=user,
                    )
    assert exc.value.status_code == 400
    assert WINDOW_CLOSED_ERROR in str(exc.value.detail)


@pytest.mark.asyncio
async def test_send_message_allows_approved_template_outside_window():
    from app.routes import messages as messages_route
    from app.models.message import MessageSend

    lead_id = ObjectId()
    user_id = ObjectId()
    tmpl_id = ObjectId()
    lead = {
        "_id": lead_id,
        "phone": "+447700900000",
        "blacklisted": False,
        "last_inbound_at": None,
        "whatsapp_window_expires_at": None,
    }
    tmpl = {
        "_id": tmpl_id,
        "user_id": str(user_id),
        "name": "Welcome",
        "content_sid": "HXabc123",
        "status": "approved",
    }
    msg_doc = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "lead_id": str(lead_id),
        "direction": "outbound",
        "message": "[template:HXabc123]",
        "status": "queued",
        "content_sid": "HXabc123",
        "created_at": datetime.now(timezone.utc),
    }
    user = {"_id": user_id}

    with (
        patch.object(messages_route.lead_service, "get_lead", new=AsyncMock(return_value=lead)),
        patch.object(messages_route, "get_approved_template", new=AsyncMock(return_value=tmpl)),
        patch.object(
            messages_route.message_service,
            "insert_message",
            new=AsyncMock(return_value=msg_doc),
        ) as insert_mock,
        patch.object(messages_route.ws_manager, "push", new=AsyncMock()),
        patch.object(messages_route, "enqueue") as enqueue_mock,
        patch("app.security.rate_limit.rate_limit_send"),
        patch("app.services.whatsapp_eligibility._sender_ok", return_value=True),
        patch.object(
            messages_route,
            "get_db",
            return_value=MagicMock(
                messages=MagicMock(
                    update_one=AsyncMock(),
                    find_one=AsyncMock(return_value=msg_doc),
                )
            ),
        ),
    ):
        result = await messages_route.send_message(
            MessageSend(
                lead_id=str(lead_id),
                template_id=str(tmpl_id),
                content_variables={"1": "Priya"},
                message_purpose="transactional",
            ),
            user=user,
        )

    assert result["status"] == "queued"
    assert insert_mock.await_args.kwargs["content_sid"] == "HXabc123"
    enqueue_mock.assert_called_once()
    assert enqueue_mock.call_args.kwargs.get("content_sid") == "HXabc123"


@pytest.mark.asyncio
async def test_non_approved_template_rejected():
    from app.routes.templates import get_approved_template

    user_id = str(ObjectId())
    tmpl_id = ObjectId()
    db = MagicMock()
    db.templates.find_one = AsyncMock(
        return_value={
            "_id": tmpl_id,
            "user_id": user_id,
            "content_sid": "HXabc",
            "status": "draft",
        }
    )
    with patch("app.routes.templates.get_db", return_value=db):
        with pytest.raises(HTTPException) as exc:
            await get_approved_template(user_id, str(tmpl_id))
    assert exc.value.status_code == 400
    assert "not approved" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_tenant_cannot_access_other_tenant_templates():
    from app.routes import templates as templates_route
    from app.models.template import TemplateUpdate

    other = ObjectId()
    tmpl_id = ObjectId()
    db = MagicMock()
    db.templates.find_one = AsyncMock(return_value=None)
    db.templates.update_one = AsyncMock(return_value=SimpleNamespace(matched_count=0))
    db.templates.delete_one = AsyncMock(return_value=SimpleNamespace(deleted_count=0))

    with patch("app.routes.templates.get_db", return_value=db):
        with pytest.raises(HTTPException) as exc_get:
            await templates_route.get_approved_template(str(other), str(tmpl_id))
        with pytest.raises(HTTPException) as exc_patch:
            await templates_route.update_template(
                str(tmpl_id),
                TemplateUpdate(name="x"),
                user={"_id": other},
            )
        with pytest.raises(HTTPException) as exc_del:
            await templates_route.delete_template(str(tmpl_id), user={"_id": other})

    assert exc_get.value.status_code == 404
    assert exc_patch.value.status_code == 404
    assert exc_del.value.status_code == 404
    # Queries always scoped by caller user_id
    assert db.templates.find_one.await_args.args[0]["user_id"] == str(other)


def test_ai_job_rechecks_window_before_send():
    from app.workers import tasks as tasks_mod

    user_id = str(ObjectId())
    lead_id = ObjectId()
    msg_id = ObjectId()

    lead_open = {
        "_id": lead_id,
        "user_id": user_id,
        "phone": "+447700900001",
        "blacklisted": False,
        "ai_paused": False,
        "takeover_by": None,
        "last_inbound_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "whatsapp_window_expires_at": datetime.now(timezone.utc) + timedelta(hours=23),
    }
    lead_closed = {
        **lead_open,
        "last_inbound_at": datetime.now(timezone.utc) - timedelta(hours=30),
        "whatsapp_window_expires_at": datetime.now(timezone.utc) - timedelta(hours=1),
    }

    db = MagicMock()
    # Sequence: initial load, after generate, before insert checks, final before send (+ extras for AI path)
    db.leads.find_one = MagicMock(
        side_effect=[lead_open, lead_open, lead_closed]
    )
    db.users.find_one = MagicMock(return_value={"company_name": "Co", "ai_settings": {}})
    db.agents.find_one = MagicMock(return_value={"prompt": "hi"})
    db.messages.find = MagicMock(
        return_value=MagicMock(
            sort=MagicMock(return_value=MagicMock(limit=MagicMock(return_value=[])))
        )
    )
    db.messages.find_one = MagicMock(return_value={"message": "hi", "direction": "inbound"})
    db.messages.count_documents = MagicMock(return_value=2)
    db.conversation_summaries.find_one = MagicMock(return_value=None)
    db.ai_events.insert_one = MagicMock()
    insert_res = SimpleNamespace(inserted_id=msg_id)
    db.messages.insert_one = MagicMock(return_value=insert_res)
    db.messages.update_one = MagicMock()
    # After cancel, worker may load message — optional
    db.leads.update_one = MagicMock()

    with (
        patch.object(tasks_mod, "_db", return_value=db),
        patch.object(tasks_mod.openai_service, "generate_reply", return_value="AI hello"),
        patch.object(tasks_mod.twilio_service, "send_whatsapp") as send_mock,
        patch.object(tasks_mod, "_publish"),
        patch("app.workers.tasks.claim_idempotency", return_value=True),
        patch("app.workers.tasks.make_idempotency_key", return_value="idem"),
        patch("app.services.ai_quota.check_quota", return_value=(True, None)),
        patch("app.services.ai_config.resolve_ai_settings", return_value={
            "enabled": True,
            "moderation_enabled": False,
            "extraction_enabled": False,
            "summaries_enabled": False,
            "ai_disallowed_topics": "",
            "model": "gpt-4o-mini",
            "fallback_model": "gpt-4o-mini",
            "temperature": 0.5,
            "max_output_tokens": 400,
        }),
        patch("app.services.ai_summary.get_summary", return_value=None),
        patch(
            "app.services.ai_context.load_conversation_context",
            return_value={"messages": [{"role": "user", "content": "hi"}], "summary_used": False},
        ),
        patch("app.services.ai_summary.maybe_enqueue_summary"),
    ):
        tasks_mod.generate_and_send_ai_reply(user_id, str(lead_id))

    send_mock.assert_not_called()
    assert db.messages.update_one.called
    update_set = db.messages.update_one.call_args[0][1]["$set"]
    assert update_set["status"] == "canceled"
    assert WINDOW_CLOSED_ERROR in update_set["error"]
