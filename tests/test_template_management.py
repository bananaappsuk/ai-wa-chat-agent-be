"""Template management: delete from the app or from WhatsApp too (never while a send uses it),
and alerts when WhatsApp pauses, disables or re-approves a live template — pausing the blasts and
campaigns that use it so they don't fail recipient by recipient."""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import mongomock
import pytest
from bson import ObjectId
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

UID = ObjectId()
USER = {"_id": UID}


async def _db_with_template(**fields):
    db = AsyncMongoMockClient().db
    tid = (await db.templates.insert_one({"user_id": str(UID), "name": "Offer", "content_sid": "HXdel", "provider": "twilio_content", **fields})).inserted_id
    return db, str(tid)


async def _delete(db, tid, remote, deleter=None):
    from app.routes import templates as t

    deleter = deleter or MagicMock()
    with patch.object(t, "get_db", return_value=db), patch("app.services.twilio_service.delete_whatsapp_template", deleter):
        await t.delete_template(tid, remote=remote, user=USER)
    return deleter


@pytest.mark.asyncio
async def test_remove_from_app_only_keeps_it_on_whatsapp():
    db, tid = await _db_with_template()
    deleter = await _delete(db, tid, remote=False)
    assert not deleter.called and await db.templates.count_documents({}) == 0


@pytest.mark.asyncio
async def test_delete_from_whatsapp_too():
    db, tid = await _db_with_template()
    deleter = await _delete(db, tid, remote=True)
    deleter.assert_called_once_with("HXdel")
    assert await db.templates.count_documents({}) == 0


@pytest.mark.asyncio
async def test_never_deletes_from_whatsapp_while_a_send_uses_it():
    db, tid = await _db_with_template()
    await db.blast_campaigns.insert_one({"user_id": str(UID), "name": "Day 2", "status": "sending", "content_sid": "HXdel"})
    with pytest.raises(HTTPException) as e:
        await _delete(db, tid, remote=True)
    assert e.value.status_code == 409 and "Day 2" in e.value.detail and await db.templates.count_documents({}) == 1
    await _delete(db, tid, remote=False)
    assert await db.templates.count_documents({}) == 0


@pytest.mark.asyncio
async def test_meta_templates_are_only_removed_from_the_app():
    db, tid = await _db_with_template(provider="meta")
    with pytest.raises(HTTPException) as e:
        await _delete(db, tid, remote=True)
    assert e.value.status_code == 400


@pytest.mark.asyncio
async def test_twilio_delete_failure_keeps_the_template():
    db, tid = await _db_with_template()
    with pytest.raises(HTTPException) as e:
        await _delete(db, tid, remote=True, deleter=MagicMock(side_effect=RuntimeError("Twilio could not delete the template: boom")))
    assert e.value.status_code == 502 and await db.templates.count_documents({}) == 1


def test_twilio_delete_also_deletes_the_whatsapp_copy_and_tolerates_already_gone():
    from app.services import twilio_service

    with patch("httpx.delete", return_value=MagicMock(status_code=404)) as d, \
         patch.object(twilio_service.settings, "TWILIO_ACCOUNT_SID", "AC1"), patch.object(twilio_service.settings, "TWILIO_AUTH_TOKEN", "t"):
        twilio_service.delete_whatsapp_template("HXgone")
    assert d.call_args.kwargs["params"] == {"deleteInWaba": "true"}


def test_paused_template_pauses_its_sends_and_alerts_then_alerts_when_back():
    from app.services import whatsapp_template_approval as w

    db = mongomock.MongoClient().db
    uid = str(UID)
    tid = db.templates.insert_one({"user_id": uid, "name": "Day 2", "content_sid": "HXlive", "status": "approved",
                                   "whatsapp_approval_status": "approved"}).inserted_id
    quiet = db.templates.insert_one({"user_id": uid, "name": "Quiet", "content_sid": "HXq", "status": "approved",
                                     "whatsapp_approval_status": "approved"}).inserted_id
    blast = db.blast_campaigns.insert_one({"user_id": uid, "status": "sending", "content_sid": "HXlive"}).inserted_id
    done = db.blast_campaigns.insert_one({"user_id": uid, "status": "completed", "content_sid": "HXlive"}).inserted_id
    camp = db.campaigns.insert_one({"user_id": uid, "status": "running", "template_id": str(tid)}).inserted_id
    state = {"HXlive": "paused", "HXq": "approved"}
    with patch("app.workers.tasks._db", return_value=db), \
         patch("app.services.twilio_service.get_content_template_info", side_effect=lambda sid: {"whatsapp_status": state[sid]}), \
         patch("app.services.notifications._publish_sync", MagicMock(), create=True):
        assert w.refresh_live_templates() == 1
        assert db.blast_campaigns.find_one({"_id": blast})["status"] == "paused"
        assert "Resume" in db.blast_campaigns.find_one({"_id": blast})["pause_reason"]
        assert db.blast_campaigns.find_one({"_id": done})["status"] == "completed"
        assert db.campaigns.find_one({"_id": camp})["status"] == "paused"
        n = db.notifications.find_one({"title": "Template paused by WhatsApp: Day 2"})
        assert n and "2 running blast/campaigns using it were paused" in n["message"]
        assert db.templates.find_one({"_id": tid})["whatsapp_approval_status"] == "paused"
        assert db.templates.find_one({"_id": quiet})["whatsapp_approval_status"] == "approved"
        state["HXlive"] = "approved"
        assert w.refresh_live_templates() == 1
    assert db.notifications.count_documents({"title": "Template approved again: Day 2"}) == 1


def test_disabled_template_leaves_the_library_as_rejected():
    from app.services import whatsapp_template_approval as w

    db = mongomock.MongoClient().db
    tid = db.templates.insert_one({"user_id": str(UID), "name": "Old", "content_sid": "HXd", "status": "approved",
                                   "whatsapp_approval_status": "paused"}).inserted_id
    with patch("app.workers.tasks._db", return_value=db), \
         patch("app.services.twilio_service.get_content_template_info", return_value={"whatsapp_status": "disabled"}), \
         patch("app.services.notifications._publish_sync", MagicMock(), create=True):
        assert w.refresh_live_templates() == 1
    doc = db.templates.find_one({"_id": tid})
    assert doc["status"] == "rejected" and doc["whatsapp_approval_status"] == "disabled"
    assert db.notifications.count_documents({"title": "Template disabled by WhatsApp: Old"}) == 1
