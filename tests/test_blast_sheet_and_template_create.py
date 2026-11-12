"""WA Blast sheet upload (clean + check + personalised names) and creating WhatsApp templates in
the app (build on Twilio, submit, track approval). Numbers are fictional (07700 900xxx)."""
from unittest.mock import AsyncMock, MagicMock, patch

import mongomock
import pytest
from bson import ObjectId
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models.campaign import BlastCreate, BlastPrepare
from app.models.template import WhatsAppTemplateCreate

UID = ObjectId()
USER = {"_id": UID, "plan": "business", "subscription_status": "active"}


async def _prepare(db, rows, template_id=None):
    from app.routes import campaigns as r

    with patch.object(r, "get_db", return_value=db):
        return await r.prepare_blast_recipients(BlastPrepare(rows=rows, template_id=template_id), user=USER)


@pytest.mark.asyncio
async def test_prepare_marks_opted_out_and_already_sent():
    db = AsyncMongoMockClient().db
    uid = str(UID)
    await db.leads.insert_one({"user_id": uid, "phone": "+447700900202", "name": "Out", "whatsapp_consent_status": "opted_out"})
    await db.blacklist.insert_one({"user_id": uid, "phone": "+447700900203"})
    tid = str((await db.templates.insert_one({"user_id": uid, "name": "Offer", "content_sid": "HXabc"})).inserted_id)
    bid = str((await db.blast_campaigns.insert_one({"user_id": uid, "content_sid": "HXabc"})).inserted_id)
    await db.blast_recipients.insert_one({"blast_id": bid, "phone": "+447700900204", "status": "delivered"})
    rows = [["First Name", "Mobile No"], ["Ann", "07700900201"], ["Out", "07700900202"], ["Blocked", "07700900203"], ["Again", "07700900204"]]
    out = await _prepare(db, rows, template_id=tid)
    assert [c["status"] for c in out["contacts"]] == ["ok", "opted_out", "opted_out", "already_sent"]
    assert out["summary"] == {"ok": 1, "opted_out": 2, "already_sent": 1}
    out2 = await _prepare(db, rows)
    assert [c["status"] for c in out2["contacts"]][3] == "ok"


@pytest.mark.asyncio
async def test_blast_with_names_greets_people_and_keeps_real_names():
    from app.routes import campaigns as r

    db = AsyncMongoMockClient().db
    uid = str(UID)
    await db.leads.insert_one({"user_id": uid, "phone": "+447700900302", "name": "+447700900302"})
    await db.leads.insert_one({"user_id": uid, "phone": "+447700900303", "name": "Dr Real Name"})
    resolved = {"provider": "twilio", "template_id": str(ObjectId()), "content_sid": "HXabc", "meta_template_name": None, "meta_language_code": None}
    with patch.object(r, "get_db", return_value=db), patch.object(r, "enqueue", MagicMock()), \
         patch("app.services.campaign_provider.resolve_campaign_template", AsyncMock(return_value=resolved)), \
         patch("app.security.rate_limit.rate_limit_campaign"), patch("app.services.throughput.assert_bulk_enqueue_allowed"):
        out = await r.create_blast(BlastCreate(
            name="Follow-up", recipients=["+447700900301", "+447700900302", "+447700900303"], template_id=resolved["template_id"],
            content_variables={"1": ""}, message_purpose="conversational",
            contacts=[{"phone": "+447700900301", "name": "Ann New"}, {"phone": "07700900302", "name": "Bob Phone"},
                      {"phone": "+447700900303", "name": "Should Not Overwrite"}],
        ), user=USER)
    assert out["total_recipients"] == 3
    names = {l["phone"]: l["name"] async for l in db.leads.find({"user_id": uid})}
    assert names == {"+447700900301": "Ann New", "+447700900302": "Bob Phone", "+447700900303": "Dr Real Name"}
    rec = {x["phone"]: x.get("name") async for x in db.blast_recipients.find({})}
    assert rec["+447700900301"] == "Ann New"


def test_greeting_uses_the_saved_first_name():
    from app.services.ai_campaign import fill_blank_greeting_variables

    assert fill_blank_greeting_variables({"1": ""}, lead={"name": "Ann New"}, template_body="Hi {{1}},\n\nThanks") == {"1": "Ann"}


def _create_payload(**kw):
    base = dict(name="GBBS day 3 offer", body="Hi {{1}}, thanks for visiting!", samples={"1": "Jane"})
    base.update(kw)
    return WhatsAppTemplateCreate(**base)


async def _create(db, payload, created):
    from app.routes import templates as t

    fake = MagicMock(return_value=created)
    with patch.object(t, "get_db", return_value=db), patch("app.services.twilio_service.create_whatsapp_template", fake):
        out = await t.create_whatsapp_template(payload, user=USER)
    return out, fake


@pytest.mark.asyncio
async def test_create_template_submits_and_stores_it_pending():
    db = AsyncMongoMockClient().db
    out, fake = await _create(db, _create_payload(), {"content_sid": "HXnew", "status": "received", "rejection_reason": None})
    kw = fake.call_args.kwargs
    assert kw["template_name"] == "gbbs_day_3_offer" and kw["samples"] == {"1": "Jane"} and kw["category"] == "MARKETING"
    doc = await db.templates.find_one({"content_sid": "HXnew"})
    assert doc["status"] == "pending" and doc["created_via"] == "app" and doc["whatsapp_approval_status"] == "under_review"
    assert out["variables"] == ["1"]
    out2, fake2 = await _create(db, _create_payload(), {"content_sid": "HXnew2", "status": "received"})
    assert fake2.call_args.kwargs["template_name"] == "gbbs_day_3_offer_2"


@pytest.mark.asyncio
async def test_create_template_buttons_become_card_actions():
    db = AsyncMongoMockClient().db
    payload = _create_payload(footer="Build, Test and Train", buttons=[
        {"type": "URL", "title": "Book a call", "url": "https://example.com/book"},
        {"type": "PHONE_NUMBER", "title": "Call us", "phone": "+447700900123"},
        {"type": "QUICK_REPLY", "title": "Interested"},
    ])
    _, fake = await _create(db, payload, {"content_sid": "HXc", "status": "received"})
    assert fake.call_args.kwargs["actions"] == [
        {"type": "URL", "title": "Book a call", "url": "https://example.com/book"},
        {"type": "PHONE_NUMBER", "title": "Call us", "phone": "+447700900123"},
        {"type": "QUICK_REPLY", "title": "Interested", "id": "interested"},
    ]


@pytest.mark.asyncio
async def test_twilio_error_is_shown_to_the_user():
    from app.routes import templates as t

    db = AsyncMongoMockClient().db
    with patch.object(t, "get_db", return_value=db), \
         patch("app.services.twilio_service.create_whatsapp_template", MagicMock(side_effect=RuntimeError("Twilio could not create the template: bad"))), \
         pytest.raises(HTTPException) as e:
        await t.create_whatsapp_template(_create_payload(), user=USER)
    assert e.value.status_code == 400 and "bad" in e.value.detail and await db.templates.count_documents({}) == 0


def test_twilio_payload_text_vs_card():
    from app.services import twilio_service

    calls = []

    def post(url, **kw):
        calls.append((url, kw.get("json")))
        return MagicMock(status_code=201, json=lambda: {"sid": "HXz", "status": "received"})

    with patch("httpx.post", side_effect=post), patch.object(twilio_service.settings, "TWILIO_ACCOUNT_SID", "AC1"), \
         patch.object(twilio_service.settings, "TWILIO_AUTH_TOKEN", "t"):
        twilio_service.create_whatsapp_template(template_name="a", language="en", category="MARKETING", body="Hi {{1}}", samples={"1": "Jane"})
        twilio_service.create_whatsapp_template(template_name="b", language="en", category="UTILITY", body="Hi", samples={}, footer="F",
                                                actions=[{"type": "QUICK_REPLY", "title": "Yes", "id": "yes"}])
    assert calls[0][1] == {"friendly_name": "a", "language": "en", "types": {"twilio/text": {"body": "Hi {{1}}"}}, "variables": {"1": "Jane"}}
    assert calls[1][1] == {"name": "a", "category": "MARKETING"}
    assert calls[2][1]["types"] == {"whatsapp/card": {"body": "Hi", "actions": [{"type": "QUICK_REPLY", "title": "Yes", "id": "yes"}], "footer": "F"}}


def test_approval_is_tracked_and_the_user_is_told():
    from datetime import datetime, timezone

    from app.services import whatsapp_template_approval as w

    db = mongomock.MongoClient().db
    uid = str(UID)
    now = datetime.now(timezone.utc)
    ok = db.templates.insert_one({"user_id": uid, "name": "Offer", "content_sid": "HX1", "created_via": "app", "status": "pending",
                                  "whatsapp_approval_status": "under_review", "created_at": now}).inserted_id
    bad = db.templates.insert_one({"user_id": uid, "name": "Bad", "content_sid": "HX2", "created_via": "app", "status": "pending",
                                   "whatsapp_approval_status": "under_review", "created_at": now}).inserted_id
    linked = db.templates.insert_one({"user_id": uid, "name": "Linked", "content_sid": "HX3", "status": "draft",
                                      "whatsapp_approval_status": "under_review", "created_at": now}).inserted_id
    info = {"HX1": {"whatsapp_status": "approved"}, "HX2": {"whatsapp_status": "rejected", "rejection_reason": "INVALID_FORMAT"}}
    with patch("app.workers.tasks._db", return_value=db), \
         patch("app.services.twilio_service.get_content_template_info", side_effect=lambda sid: info[sid]), \
         patch("app.services.notifications._publish_sync", MagicMock(), create=True):
        assert w.refresh_app_created_templates() == 2
        assert w.refresh_app_created_templates() == 0
    assert db.templates.find_one({"_id": ok})["status"] == "approved"
    assert db.templates.find_one({"_id": bad})["status"] == "rejected"
    assert db.templates.find_one({"_id": linked})["status"] == "draft"
    titles = sorted(n["title"] for n in db.notifications.find({"user_id": uid}))
    assert titles == ["Template approved: Offer", "Template rejected: Bad"]
    assert "INVALID_FORMAT" in db.notifications.find_one({"title": "Template rejected: Bad"})["message"]
