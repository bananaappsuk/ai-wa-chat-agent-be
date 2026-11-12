"""WhatsApp senders are kept in the app (synced from Twilio) and chosen per send — no env change or
restart to switch numbers. Replies go out from the number the contact wrote to; inbound messages
reach the account on any of its numbers. Numbers are fictional (07700 900xxx)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import mongomock
import pytest
from bson import ObjectId
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.services import senders

UID = ObjectId()
A, B, C = "+447700900501", "+447700900502", "+447700900503"


def _twilio(phone, status="ONLINE", quality="HIGH", ok=True):
    return {"phone": phone, "sender_sid": "XE" + phone[-3:], "display_name": "AI Agent Hub", "twilio_status": status, "quality": quality,
            "messaging_limit": None, "callback_url": "x", "status_callback_url": "y", "webhook_ok": ok}


@pytest.mark.asyncio
async def test_sync_brings_numbers_in_and_keeps_your_settings():
    db = AsyncMongoMockClient().db
    uid = str(UID)
    with patch.object(senders, "fetch_twilio_senders", return_value=[_twilio(A), _twilio(B), _twilio(C, status="OFFLINE")]), \
         patch.object(senders.settings, "TWILIO_WHATSAPP_FROM", f"whatsapp:{B}"):
        out = await senders.sync_senders(db, uid)
    by = {d["phone"]: d for d in out}
    assert by[A]["enabled"] and by[B]["enabled"] and not by[C]["enabled"]
    assert by[B]["is_default"] and not by[A]["is_default"]
    await db.whatsapp_senders.update_one({"phone": A}, {"$set": {"label": "Show number", "enabled": False}})
    with patch.object(senders, "fetch_twilio_senders", return_value=[_twilio(A, quality="LOW"), _twilio(B)]):
        out = await senders.sync_senders(db, uid)
    by = {d["phone"]: d for d in out}
    assert by[A]["label"] == "Show number" and by[A]["enabled"] is False and by[A]["quality"] == "LOW"
    assert by[C]["twilio_status"] == "NOT_IN_TWILIO"


@pytest.mark.asyncio
async def test_another_accounts_number_is_never_taken_over():
    db = AsyncMongoMockClient().db
    await db.whatsapp_senders.insert_one({"user_id": "someone-else", "phone": A, "enabled": True})
    with patch.object(senders, "fetch_twilio_senders", return_value=[_twilio(A)]):
        out = await senders.sync_senders(db, str(UID))
    assert out == [] and (await db.whatsapp_senders.find_one({"phone": A}))["user_id"] == "someone-else"


@pytest.mark.asyncio
async def test_resolve_sender_uses_your_choice_or_the_default():
    db = AsyncMongoMockClient().db
    uid = str(UID)
    await db.whatsapp_senders.insert_many([{"user_id": uid, "phone": A, "enabled": True, "is_default": True},
                                           {"user_id": uid, "phone": B, "enabled": False, "is_default": False},
                                           {"user_id": "other", "phone": C, "enabled": True, "is_default": True}])
    assert await senders.resolve_sender(db, uid, None) == A
    assert await senders.resolve_sender(db, uid, "07700 900501") == A
    with pytest.raises(ValueError, match="switched off"):
        await senders.resolve_sender(db, uid, B)
    with pytest.raises(ValueError, match="isn't one of"):
        await senders.resolve_sender(db, uid, C)
    assert await senders.resolve_sender(AsyncMongoMockClient().db, uid, None) is None


def test_replies_go_from_the_number_the_contact_wrote_to():
    db = mongomock.MongoClient().db
    uid = str(UID)
    db.whatsapp_senders.insert_many([{"user_id": uid, "phone": A, "enabled": True, "is_default": True},
                                     {"user_id": uid, "phone": B, "enabled": True, "is_default": False},
                                     {"user_id": uid, "phone": C, "enabled": False, "is_default": False}])
    assert senders.reply_from_sync(db, uid, {"last_business_number": B}) == B
    assert senders.reply_from_sync(db, uid, {"last_business_number": C}) == A
    assert senders.reply_from_sync(db, uid, {}) == A
    assert senders.reply_from_sync(mongomock.MongoClient().db, uid, {"last_business_number": B}) is None


def test_send_uses_the_chosen_number_instead_of_env():
    from app.services import twilio_service

    client = MagicMock()
    client.messages.create.return_value = MagicMock(sid="SM1", status="queued")
    with patch.object(twilio_service, "_client", return_value=client), \
         patch.object(twilio_service.settings, "TWILIO_ACCOUNT_SID", "AC1"), patch.object(twilio_service.settings, "TWILIO_AUTH_TOKEN", "t"), \
         patch.object(twilio_service.settings, "TWILIO_WHATSAPP_FROM", f"whatsapp:{C}"):
        twilio_service.send_whatsapp("+447700900111", body="hi", from_number=A)
        twilio_service.send_whatsapp("+447700900111", body="hi")
    assert client.messages.create.call_args_list[0].kwargs["from_"] == f"whatsapp:{A}"
    assert client.messages.create.call_args_list[1].kwargs["from_"] == f"whatsapp:{C}"


@pytest.mark.asyncio
async def test_inbound_reaches_the_account_on_any_of_its_numbers():
    from app.services.inbound_whatsapp import InboundMessage, _resolve_tenant

    db = AsyncMongoMockClient().db
    await db.users.insert_one({"_id": UID, "email": "a@example.com", "twilio_whatsapp_to": A})
    await db.whatsapp_senders.insert_one({"user_id": str(UID), "phone": B, "enabled": True})
    msg = lambda to: InboundMessage(provider="twilio", provider_message_id="SMx", customer_phone="+447700900999", business_identifier=to,
                                    body="hi", profile_name=None, timestamp=None, message_type="text")
    assert (await _resolve_tenant(db, msg(B)))["_id"] == UID
    assert (await _resolve_tenant(db, msg(A)))["_id"] == UID
    assert await _resolve_tenant(db, msg(C)) is None


@pytest.mark.asyncio
async def test_inbound_records_the_number_written_to_and_ignores_our_own_numbers():
    from app.services import inbound_whatsapp
    from app.services.inbound_whatsapp import InboundMessage, process_inbound_message

    db = AsyncMongoMockClient().db
    await db.users.insert_one({"_id": UID, "email": "a@example.com"})
    await db.whatsapp_senders.insert_many([{"user_id": str(UID), "phone": A, "enabled": True}, {"user_id": str(UID), "phone": B, "enabled": True}])
    with patch("app.services.lead_service.get_db", return_value=db), patch("app.services.lead_scoring.get_db", return_value=db), \
         patch.object(inbound_whatsapp.ws_manager, "push", new=AsyncMock()):
        await process_inbound_message(InboundMessage(provider="twilio", provider_message_id="SM1", customer_phone="+447700900777",
                                                     business_identifier=B, body="hello", profile_name="Jane", timestamp=None,
                                                     message_type="text", skip_provider_outbound=True, skip_ai_jobs=True), db=db)
        loop = await process_inbound_message(InboundMessage(provider="twilio", provider_message_id="SM2", customer_phone=A,
                                                            business_identifier=B, body="echo", profile_name=None, timestamp=None,
                                                            message_type="text", skip_provider_outbound=True, skip_ai_jobs=True), db=db)
    lead = await db.leads.find_one({"phone": "+447700900777"})
    assert lead["last_business_number"] == B
    assert (await db.messages.find_one({"lead_id": str(lead["_id"])}))["business_number"] == B
    assert await db.leads.find_one({"phone": A}) is None and loop.outcome != "processed"


@pytest.mark.asyncio
async def test_blast_saves_the_chosen_sender_and_rejects_others():
    from app.models.campaign import BlastCreate
    from app.routes import campaigns as r

    db = AsyncMongoMockClient().db
    uid = str(UID)
    await db.whatsapp_senders.insert_many([{"user_id": uid, "phone": A, "enabled": True, "is_default": True},
                                           {"user_id": uid, "phone": B, "enabled": True, "is_default": False}])
    resolved = {"provider": "twilio", "template_id": str(ObjectId()), "content_sid": "HXa", "meta_template_name": None, "meta_language_code": None}

    async def make(sender):
        with patch.object(r, "get_db", return_value=db), patch.object(r, "enqueue", MagicMock()), \
             patch("app.services.campaign_provider.resolve_campaign_template", AsyncMock(return_value=resolved)), \
             patch("app.security.rate_limit.rate_limit_campaign"), patch("app.services.throughput.assert_bulk_enqueue_allowed"):
            return await r.create_blast(BlastCreate(name="x", recipients=["+447700900111"], template_id=resolved["template_id"],
                                                    message_purpose="conversational", sender=sender), user={"_id": UID, "plan": "business", "subscription_status": "active"})

    assert (await make(B))["sender_number"] == B
    assert (await make(None))["sender_number"] == A
    with pytest.raises(HTTPException) as e:
        await make(C)
    assert e.value.status_code == 400


def test_blast_worker_sends_from_the_blasts_number():
    from app.workers import tasks

    db = mongomock.MongoClient().db
    uid = str(UID)
    db.users.insert_one({"_id": UID, "email": "t@example.com", "plan": "business", "subscription_status": "active"})
    bid = str(db.blast_campaigns.insert_one({"user_id": uid, "name": "x", "provider": "twilio", "content_sid": "HXa", "content_variables": None,
                                             "message_purpose": "conversational", "status": "sending", "total_recipients": 1,
                                             "sender_number": B, "created_at": datetime.now(timezone.utc)}).inserted_id)
    db.blast_recipients.insert_one({"blast_id": bid, "user_id": uid, "phone": "+447700900111", "status": "pending", "attempt_count": 0,
                                    "created_at": datetime.now(timezone.utc)})
    send = MagicMock(return_value={"sid": "SM1", "status": "queued"})
    with patch.object(tasks, "_db", return_value=db), patch.object(tasks.twilio_service, "send_whatsapp", send), \
         patch.object(tasks.twilio_service, "get_content_template_info", return_value={"body": "Hello", "whatsapp_status": "approved"}), \
         patch.object(tasks, "acquire_send_permit", return_value=True), patch.object(tasks, "_publish"), \
         patch("app.workers.queue.get_queue", return_value=MagicMock()):
        tasks.send_blast_messages(uid, bid)
    assert send.call_args.kwargs["from_number"] == B


@pytest.mark.asyncio
async def test_changing_the_default_and_switching_numbers_off():
    from app.routes import senders as route

    db = AsyncMongoMockClient().db
    uid = str(UID)
    a = str((await db.whatsapp_senders.insert_one({"user_id": uid, "phone": A, "enabled": True, "is_default": True})).inserted_id)
    b = str((await db.whatsapp_senders.insert_one({"user_id": uid, "phone": B, "enabled": True, "is_default": False})).inserted_id)
    c = str((await db.whatsapp_senders.insert_one({"user_id": uid, "phone": C, "enabled": False, "is_default": False})).inserted_id)
    user = {"_id": UID}
    with patch.object(route, "get_db", return_value=db):
        out = await route.update_sender(b, route.SenderUpdate(is_default=True), user=user)
        assert [s["phone"] for s in out["senders"] if s["is_default"]] == [B]
        with pytest.raises(HTTPException):
            await route.update_sender(c, route.SenderUpdate(is_default=True), user=user)
        out = await route.update_sender(b, route.SenderUpdate(enabled=False), user=user)
        assert not any(s["is_default"] for s in out["senders"])
        out = await route.update_sender(a, route.SenderUpdate(label="Main number"), user=user)
        assert next(s for s in out["senders"] if s["phone"] == A)["label"] == "Main number"
        with pytest.raises(HTTPException):
            await route.update_sender(a, route.SenderUpdate(label="x"), user={"_id": ObjectId()})


def test_fix_webhook_points_the_number_at_the_app():
    with patch("httpx.post", return_value=MagicMock(status_code=200)) as post, \
         patch.object(senders.settings, "PUBLIC_BASE_URL", "https://app.example.com"), \
         patch.object(senders.settings, "TWILIO_ACCOUNT_SID", "AC1"), patch.object(senders.settings, "TWILIO_AUTH_TOKEN", "t"):
        senders.fix_twilio_webhook("XE1")
    assert post.call_args.args[0].endswith("/Senders/XE1")
    assert post.call_args.kwargs["json"]["webhook"] == {"callback_url": "https://app.example.com/api/webhook/whatsapp", "callback_method": "POST",
                                                        "status_callback_url": "https://app.example.com/api/webhook/twilio/status", "status_callback_method": "POST"}


def test_number_check_alerts_on_offline_quality_and_webhook_changes():
    db = mongomock.MongoClient().db
    uid = str(UID)
    db.whatsapp_senders.insert_many([
        {"user_id": uid, "phone": A, "enabled": True, "is_default": True, "twilio_status": "ONLINE", "quality": "HIGH", "webhook_ok": True},
        {"user_id": uid, "phone": B, "enabled": True, "is_default": False, "twilio_status": "ONLINE", "quality": "LOW", "webhook_ok": True},
        {"user_id": uid, "phone": C, "enabled": True, "is_default": False, "twilio_status": "ONLINE", "quality": "HIGH", "webhook_ok": True},
    ])
    live = [_twilio(A, status="OFFLINE"), _twilio(B, quality="MEDIUM", ok=False), _twilio(C, quality="LOW")]
    with patch("app.workers.tasks._db", return_value=db), patch.object(senders, "fetch_twilio_senders", return_value=live), \
         patch("app.services.notifications._publish_sync", MagicMock(), create=True):
        assert senders.refresh_senders_sync() == 4
        assert senders.refresh_senders_sync() == 0
    titles = sorted(n["title"] for n in db.notifications.find({}))
    assert titles == ["Quality back to MEDIUM", "Quality dropped to LOW", "Replies to a number aren't reaching the app", "WhatsApp number offline"]
    assert "default sender" in db.notifications.find_one({"title": "WhatsApp number offline"})["message"]
    assert db.whatsapp_senders.find_one({"phone": A})["twilio_status"] == "OFFLINE"


@pytest.mark.asyncio
async def test_a_restricted_number_is_switched_off_and_sends_move_to_another():
    db = AsyncMongoMockClient().db
    uid = str(UID)
    await db.whatsapp_senders.insert_many([
        {"user_id": uid, "phone": A, "enabled": True, "is_default": True, "twilio_status": "ONLINE"},
        {"user_id": uid, "phone": B, "enabled": True, "is_default": False, "twilio_status": "ONLINE"},
    ])
    blast = (await db.blast_campaigns.insert_one({"user_id": uid, "status": "sending", "sender_number": A})).inserted_id
    other = (await db.blast_campaigns.insert_one({"user_id": uid, "status": "sending", "sender_number": B})).inserted_id
    with patch("app.services.notifications.ws_manager", MagicMock(push=AsyncMock()), create=True):
        assert await senders.mark_sender_restricted(db, f"whatsapp:{A}", "63051") is True
        assert await senders.mark_sender_restricted(db, f"whatsapp:{A}", "63051") is False
        assert await senders.mark_sender_restricted(db, "whatsapp:+447700900999", "63051") is False
    a = await db.whatsapp_senders.find_one({"phone": A})
    assert a["restricted"] and not a["enabled"] and not a["is_default"]
    assert (await db.whatsapp_senders.find_one({"phone": B}))["is_default"] is True
    assert (await db.blast_campaigns.find_one({"_id": blast}))["status"] == "paused"
    assert (await db.blast_campaigns.find_one({"_id": other}))["status"] == "sending"
    n = await db.notifications.find_one({"title": f"WhatsApp blocked a number: {A}"})
    assert n and f"New sends now go from {B}" in n["message"] and "1 running blast/campaign using it was paused" in n["message"]


@pytest.mark.asyncio
async def test_switching_a_restricted_number_back_on_clears_the_flag():
    from app.routes import senders as route

    db = AsyncMongoMockClient().db
    sid = str((await db.whatsapp_senders.insert_one({"user_id": str(UID), "phone": A, "enabled": False, "restricted": True})).inserted_id)
    with patch.object(route, "get_db", return_value=db):
        await route.update_sender(sid, route.SenderUpdate(enabled=True), user={"_id": UID})
    doc = await db.whatsapp_senders.find_one({"phone": A})
    assert doc["enabled"] and not doc["restricted"]


def test_delivery_receipt_with_63051_marks_the_number():
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import webhook as wh

    @asynccontextmanager
    async def _noop(_app):
        yield

    app.router.lifespan_context = _noop
    marked = AsyncMock(return_value=True)
    with patch.object(wh.twilio_service, "validate_signature", return_value=True), \
         patch.object(wh, "apply_twilio_status_callback", AsyncMock()), patch.object(wh.senders, "mark_sender_restricted", marked), \
         patch.object(wh, "get_db", return_value=MagicMock()), patch("app.security.rate_limit.rate_limit_webhook"), TestClient(app) as c:
        r1 = c.post("/api/webhook/twilio/status", data={"MessageSid": "SM1", "MessageStatus": "undelivered", "ErrorCode": "63051", "From": f"whatsapp:{A}"})
        r2 = c.post("/api/webhook/twilio/status", data={"MessageSid": "SM2", "MessageStatus": "undelivered", "ErrorCode": "63024", "From": f"whatsapp:{A}"})
    assert r1.status_code == 200 and r2.status_code == 200
    assert marked.await_count == 1 and marked.await_args.args[1:] == (f"whatsapp:{A}", "63051")


@pytest.mark.asyncio
async def test_sync_marks_a_number_whose_last_send_was_refused_by_meta():
    db = AsyncMongoMockClient().db
    uid = str(UID)
    await db.whatsapp_senders.insert_one({"user_id": uid, "phone": A, "enabled": True, "is_default": True, "label": "", "twilio_status": "ONLINE"})
    live = [{**_twilio(A), "last_error_code": "63051"}, {**_twilio(B), "last_error_code": None}]
    with patch.object(senders, "fetch_twilio_senders", return_value=live):
        out = await senders.sync_senders(db, uid)
    by = {d["phone"]: d for d in out}
    assert by[A]["restricted"] and by[A]["restricted_code"] == "63051" and not by[A]["enabled"] and not by[A]["is_default"]
    assert not by[B].get("restricted") and by[B]["is_default"] and by[B]["label"] == ""


def test_number_check_alerts_once_when_meta_restricts_a_number():
    db = mongomock.MongoClient().db
    uid = str(UID)
    db.whatsapp_senders.insert_one({"user_id": uid, "phone": A, "enabled": True, "is_default": True, "twilio_status": "ONLINE", "quality": "HIGH", "webhook_ok": True})
    with patch("app.workers.tasks._db", return_value=db), patch.object(senders, "fetch_twilio_senders", return_value=[{**_twilio(A), "last_error_code": "63051"}]), \
         patch("app.services.notifications._publish_sync", MagicMock(), create=True):
        assert senders.refresh_senders_sync() == 1
        assert senders.refresh_senders_sync() == 0
    doc = db.whatsapp_senders.find_one({"phone": A})
    assert doc["restricted"] and not doc["enabled"]
    assert db.notifications.count_documents({"title": f"WhatsApp blocked a number: {A}"}) == 1
