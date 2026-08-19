"""Phase 2C Live Chat human outbound for Meta + Twilio (no live Graph/Twilio)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.models.message import MessageSend
from app.routes import messages as messages_route
from app.services.inbound_whatsapp import resolve_lead_whatsapp_provider
from app.services.meta_whatsapp_service import MetaWhatsAppError
from app.workers import tasks


def _open_lead(user_id: str, lead_id: ObjectId, **extra) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "_id": lead_id,
        "user_id": user_id,
        "phone": "+447700900123",
        "blacklisted": False,
        "ai_paused": False,
        "takeover_by": None,
        "whatsapp_consent_status": "opted_in",
        "last_inbound_at": now - timedelta(hours=1),
        "whatsapp_window_expires_at": now + timedelta(hours=23),
    }
    doc.update(extra)
    return doc


def _closed_lead(user_id: str, lead_id: ObjectId) -> dict:
    now = datetime.now(timezone.utc)
    return _open_lead(
        user_id,
        lead_id,
        last_inbound_at=now - timedelta(hours=30),
        whatsapp_window_expires_at=now - timedelta(hours=1),
    )


async def _send(lead, user, payload, inbound, *, expect_ok=True):
    insert_doc = {
        "_id": ObjectId(),
        "user_id": str(user["_id"]),
        "lead_id": str(lead["_id"]),
        "direction": "outbound",
        "message": payload.message or "",
        "status": "queued",
        "created_at": datetime.now(timezone.utc),
    }
    enqueue = MagicMock()
    push = AsyncMock()
    insert = AsyncMock(return_value=insert_doc)

    async def find_one(query=None, **kwargs):
        if (query or {}).get("direction") == "inbound":
            return inbound
        return {
            **insert_doc,
            "provider": "meta" if (inbound or {}).get("provider") == "meta" else "twilio",
            "sender_type": "human",
            "provider_message_id": None,
        }

    db = MagicMock()
    db.messages = MagicMock(update_one=AsyncMock(), find_one=AsyncMock(side_effect=find_one))
    with (
        patch.object(messages_route.lead_service, "get_lead", new=AsyncMock(return_value=lead)),
        patch.object(messages_route.message_service, "insert_message", new=insert),
        patch.object(messages_route.ws_manager, "push", new=push),
        patch.object(messages_route, "enqueue", enqueue),
        patch("app.security.rate_limit.rate_limit_send"),
        patch("app.services.whatsapp_eligibility._sender_ok", return_value=True),
        patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True),
        patch.object(messages_route, "get_db", return_value=db),
        patch.object(
            messages_route,
            "get_approved_template",
            new=AsyncMock(
                return_value={
                    "_id": ObjectId(),
                    "content_sid": "HXabc",
                    "name": "Welcome",
                    "status": "approved",
                }
            ),
        ),
    ):
        if expect_ok:
            result = await messages_route.send_message(payload, user=user)
            return result, enqueue, push, db, insert
        with pytest.raises(HTTPException) as exc:
            await messages_route.send_message(payload, user=user)
        return exc.value, enqueue, push, db, insert


@pytest.mark.asyncio
async def test_resolve_scopes_latest_inbound_only():
    db = MagicMock()
    db.messages.find_one = AsyncMock(
        return_value={"direction": "inbound", "provider": "meta", "user_id": "u1", "lead_id": "l1"}
    )
    assert await resolve_lead_whatsapp_provider("u1", "l1", db=db) == "meta"
    q = db.messages.find_one.await_args.args[0]
    sort = db.messages.find_one.await_args.kwargs["sort"]
    assert q == {"user_id": "u1", "lead_id": "l1", "direction": "inbound"}
    assert sort == [("created_at", -1)]


@pytest.mark.asyncio
async def test_resolve_missing_or_legacy_defaults_twilio():
    db = MagicMock()
    db.messages.find_one = AsyncMock(return_value=None)
    assert await resolve_lead_whatsapp_provider("u1", "l1", db=db) == "twilio"
    db.messages.find_one = AsyncMock(return_value={"direction": "inbound", "message": "hi"})
    assert await resolve_lead_whatsapp_provider("u1", "l1", db=db) == "twilio"
    db.messages.find_one = AsyncMock(
        return_value={"direction": "inbound", "provider": "twilio"}
    )
    assert await resolve_lead_whatsapp_provider("u1", "l1", db=db) == "twilio"


@pytest.mark.asyncio
async def test_meta_inbound_queues_human_meta_text():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    result, enq, push, db, insert = await _send(
        lead,
        user,
        MessageSend(lead_id=str(lead_id), message="hello from ops"),
        {"direction": "inbound", "provider": "meta"},
    )
    assert result["status"] == "queued"
    assert result.get("provider") == "meta"
    assert result.get("sender_type") == "human"
    assert insert.await_args.kwargs["provider"] == "meta"
    assert insert.await_args.kwargs["sender_type"] == "human"
    assert insert.await_args.kwargs["provider_message_id"] is None
    extra = db.messages.update_one.await_args.args[1]["$set"]
    assert extra["provider"] == "meta"
    assert extra["sender_type"] == "human"
    assert extra["provider_message_id"] is None
    assert "sender_number" not in extra
    assert "trigger_message_id" not in extra
    push.assert_awaited()
    assert push.await_args.args[1] == "message:new"
    enq.assert_called_once()
    assert enq.call_args.args[0] is tasks.send_outbound_message


@pytest.mark.asyncio
async def test_meta_closed_window_400_no_queue_no_twilio_template():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _closed_lead(str(user["_id"]), lead_id)
    err, enq, push, db, insert = await _send(
        lead,
        user,
        MessageSend(lead_id=str(lead_id), message="too late"),
        {"direction": "inbound", "provider": "meta"},
        expect_ok=False,
    )
    assert err.status_code == 400
    detail = str(err.detail)
    assert "Meta template" in detail
    assert "Content SID" not in detail
    enq.assert_not_called()
    insert.assert_not_called()
    push.assert_not_called()


@pytest.mark.asyncio
async def test_meta_media_rejected():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    err, enq, _, _, insert = await _send(
        lead,
        user,
        MessageSend(
            lead_id=str(lead_id),
            media_url="https://example.com/x.jpg",
            media_content_type="image/jpeg",
        ),
        {"direction": "inbound", "provider": "meta"},
        expect_ok=False,
    )
    assert err.status_code == 400
    assert "Media" in str(err.detail)
    enq.assert_not_called()
    insert.assert_not_called()


@pytest.mark.asyncio
async def test_meta_template_and_content_sid_rejected():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    err, enq, _, _, insert = await _send(
        lead,
        user,
        MessageSend(
            lead_id=str(lead_id),
            template_id=str(ObjectId()),
            message_purpose="transactional",
        ),
        {"direction": "inbound", "provider": "meta"},
        expect_ok=False,
    )
    assert err.status_code == 400
    assert "Template" in str(err.detail)
    enq.assert_not_called()
    insert.assert_not_called()

    err2, enq2, _, _, insert2 = await _send(
        lead,
        user,
        MessageSend(
            lead_id=str(lead_id),
            content_sid="HXabc",
            message_purpose="transactional",
        ),
        {"direction": "inbound", "provider": "meta"},
        expect_ok=False,
    )
    assert err2.status_code == 400
    assert "Template" in str(err2.detail)
    enq2.assert_not_called()
    insert2.assert_not_called()


@pytest.mark.asyncio
async def test_twilio_text_still_works():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    result, enq, _, db, insert = await _send(
        lead,
        user,
        MessageSend(lead_id=str(lead_id), message="twilio hi"),
        {"direction": "inbound", "provider": "twilio"},
    )
    assert result["status"] == "queued"
    extra = db.messages.update_one.await_args.args[1]["$set"]
    assert extra["provider"] == "twilio"
    assert extra["sender_type"] == "human"
    assert "sender_number" in extra
    assert insert.await_args.kwargs["provider"] == "twilio"
    enq.assert_called_once()


@pytest.mark.asyncio
async def test_twilio_media_and_template_still_work():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    result, enq, _, _, _ = await _send(
        lead,
        user,
        MessageSend(
            lead_id=str(lead_id),
            media_url="https://example.com/x.jpg",
            media_content_type="image/jpeg",
            media_filename="x.jpg",
        ),
        {"direction": "inbound", "provider": "twilio"},
    )
    assert result["status"] == "queued"
    assert enq.call_args.kwargs.get("media_url")

    result2, enq2, _, _, _ = await _send(
        lead,
        user,
        MessageSend(
            lead_id=str(lead_id),
            template_id=str(ObjectId()),
            message_purpose="transactional",
        ),
        None,
    )
    assert result2["status"] == "queued"
    assert enq2.call_args.kwargs.get("content_sid") == "HXabc"


@pytest.mark.asyncio
async def test_latest_inbound_meta_wins_over_twilio_history():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    _, _, _, db, insert = await _send(
        lead,
        user,
        MessageSend(lead_id=str(lead_id), message="reply"),
        {"direction": "inbound", "provider": "meta"},
    )
    assert insert.await_args.kwargs["provider"] == "meta"


@pytest.mark.asyncio
async def test_latest_inbound_twilio_wins_over_meta_history():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    _, _, _, db, insert = await _send(
        lead,
        user,
        MessageSend(lead_id=str(lead_id), message="reply"),
        {"direction": "inbound", "provider": "twilio"},
    )
    assert insert.await_args.kwargs["provider"] == "twilio"


@pytest.mark.asyncio
async def test_no_inbound_defaults_twilio():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    _, _, _, db, insert = await _send(
        lead,
        user,
        MessageSend(lead_id=str(lead_id), message="first outreach"),
        None,
    )
    assert insert.await_args.kwargs["provider"] == "twilio"


@pytest.mark.asyncio
async def test_other_tenant_lead_404():
    with (
        patch.object(messages_route.lead_service, "get_lead", new=AsyncMock(return_value=None)),
        patch("app.security.rate_limit.rate_limit_send"),
    ):
        with pytest.raises(HTTPException) as exc:
            await messages_route.send_message(
                MessageSend(lead_id=str(ObjectId()), message="x"),
                user={"_id": ObjectId()},
            )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_manual_send_does_not_set_takeover_or_pause():
    lead_id = ObjectId()
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), lead_id)
    with patch.object(messages_route.lead_service, "set_lead_control", new=AsyncMock()) as ctrl:
        await _send(
            lead,
            user,
            MessageSend(lead_id=str(lead_id), message="ops"),
            {"direction": "inbound", "provider": "meta"},
        )
    ctrl.assert_not_called()


def test_meta_worker_sends_meta_not_twilio_and_emits_updated():
    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    message_id = str(ObjectId())
    sent_doc = {
        "_id": ObjectId(message_id),
        "user_id": user_id,
        "provider": "meta",
        "message": "hello",
        "status": "sent",
        "provider_message_id": "wamid.H",
        "sender_type": "human",
    }
    db = MagicMock()
    db.messages.find_one = MagicMock(
        side_effect=[
            {
                "_id": ObjectId(message_id),
                "user_id": user_id,
                "provider": "meta",
                "message": "hello",
                "status": "queued",
                "message_purpose": "conversational",
                "sender_type": "human",
            },
            sent_doc,
        ]
    )
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, ObjectId(lead_id)))
    db.users.find_one = MagicMock(
        return_value={"_id": ObjectId(user_id), "meta_phone_number_id": "PN_A"}
    )
    db.messages.update_one = MagicMock()
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(tasks, "release_send_permit"),
        patch.object(
            tasks,
            "get_whatsapp_send_eligibility",
            return_value=SimpleNamespace(
                allowed=True,
                reason_code="ok",
                safe_message="",
                consent_status="opted_in",
                window_status="open",
            ),
        ),
        patch.object(
            tasks,
            "send_whatsapp_text",
            return_value={"provider": "meta", "provider_message_id": "wamid.H", "status": "sent"},
        ) as send,
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "_publish") as pub,
    ):
        tasks.send_outbound_message(message_id, user_id, lead_id, body="hello")
    send.assert_called_once()
    assert send.call_args.kwargs["provider"] == "meta"
    twilio.assert_not_called()
    fields = db.messages.update_one.call_args[0][1]["$set"]
    assert fields.get("provider_message_id") == "wamid.H"
    assert fields.get("status") == "sent"
    assert "twilio_sid" not in fields
    pub.assert_called()
    assert pub.call_args.args[1] == "message:updated"


def test_meta_worker_graph_failure_no_twilio():
    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    message_id = str(ObjectId())
    queued = {
        "_id": ObjectId(message_id),
        "user_id": user_id,
        "provider": "meta",
        "message": "hello",
        "status": "queued",
        "message_purpose": "conversational",
    }
    failed = {**queued, "status": "failed"}
    db = MagicMock()
    db.messages.find_one = MagicMock(side_effect=[queued, failed])
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, ObjectId(lead_id)))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id)})
    db.messages.update_one = MagicMock()
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(tasks, "release_send_permit"),
        patch.object(
            tasks,
            "get_whatsapp_send_eligibility",
            return_value=SimpleNamespace(
                allowed=True,
                reason_code="ok",
                safe_message="",
                consent_status="opted_in",
                window_status="open",
            ),
        ),
        patch.object(
            tasks,
            "send_whatsapp_text",
            side_effect=MetaWhatsAppError("rejected", status_code=400),
        ),
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "_publish"),
    ):
        tasks.send_outbound_message(message_id, user_id, lead_id, body="hello")
    twilio.assert_not_called()
    sets = [c[0][1].get("$set", {}) for c in db.messages.update_one.call_args_list]
    assert any(s.get("status") == "failed" for s in sets)
    assert not any(s.get("provider") == "twilio" for s in sets)
