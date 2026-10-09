"""When the WhatsApp provider rejects the whole account mid-send (Twilio account inactive / out of
balance), blasts and campaigns pause instead of failing every remaining contact; Resume and
"Retry failed" then send to exactly the people who didn't get it."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import mongomock
import pytest
from bson import ObjectId
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient
from twilio.base.exceptions import TwilioRestException

from app.services.campaign_provider import account_blocked_reason, classify_bulk_send_error

UID = str(ObjectId())
SID = "HX32305b4f57a9c184aba2c60e8f70f9fa"


def _inactive():
    return TwilioRestException(401, "https://api.twilio.com/2010-04-01/Accounts/AC/Messages.json",
                               "Unable to create record: authentication failed, account ACed78 with status 4 is not active",
                               code=20003)


@pytest.mark.parametrize("exc, blocked", [
    (_inactive(), True),
    (RuntimeError("Twilio error 63024: Invalid message recipient"), False),
    (RuntimeError("Template variable {{1}} is empty — fill it in on the campaign and send again."), False),
    (RuntimeError("Contact has opted out of WhatsApp messages"), False),
])
def test_only_account_level_errors_pause_a_send(exc, blocked):
    category = classify_bulk_send_error(exc, provider="twilio")
    assert bool(account_blocked_reason(exc, category=category, provider="twilio")) is blocked


# ── Blast: the real sender, Twilio goes inactive after 2 sends, then comes back ───────────

def _blast_db(n=5):
    db = mongomock.MongoClient().db
    now = datetime.now(timezone.utc)
    db.users.insert_one({"_id": ObjectId(UID), "email": "t@example.com", "plan": "business", "subscription_status": "active"})
    bid = db.blast_campaigns.insert_one({"user_id": UID, "name": "Meetup invite", "provider": "twilio", "content_sid": SID,
                                         "content_variables": None, "message_purpose": "conversational",
                                         "status": "sending", "total_recipients": n, "created_at": now}).inserted_id
    for i in range(n):
        db.blast_recipients.insert_one({"blast_id": str(bid), "user_id": UID, "phone": f"+44770090010{i}", "status": "pending",
                                        "attempt_count": 0, "created_at": now + timedelta(seconds=i)})
    return db, str(bid)


def _run_blast(db, bid, send):
    from app.workers import tasks

    with patch.object(tasks, "_db", return_value=db), \
         patch.object(tasks.twilio_service, "send_whatsapp", side_effect=send), \
         patch.object(tasks.twilio_service, "get_content_template_info",
                      return_value={"body": "Invitation to AI Engineer in Test Meetup – London", "whatsapp_status": "approved"}), \
         patch.object(tasks, "acquire_send_permit", return_value=True), \
         patch.object(tasks, "_publish"), \
         patch("app.workers.queue.get_queue", return_value=MagicMock()):
        tasks.send_blast_messages(UID, bid)


def test_blast_pauses_when_twilio_account_goes_inactive_then_resume_sends_the_rest_once():
    db, bid = _blast_db(5)
    sent = []

    def flaky(phone, **kw):
        if len(sent) >= 2:
            raise _inactive()
        sent.append(phone)
        return {"sid": f"SM{len(sent)}", "status": "queued"}

    _run_blast(db, bid, flaky)
    blast = db.blast_campaigns.find_one({"_id": ObjectId(bid)})
    statuses = sorted(r["status"] for r in db.blast_recipients.find({"blast_id": bid}))
    assert blast["status"] == "paused" and "Twilio rejected the account" in blast["pause_reason"]
    assert statuses == ["pending", "pending", "pending", "sent", "sent"]  # nobody marked failed
    assert all(r["attempt_count"] == 0 for r in db.blast_recipients.find({"blast_id": bid, "status": "pending"}))
    assert db.notifications.count_documents({"user_id": UID, "title": "Blast paused — WhatsApp account problem"}) == 1
    assert db.leads.count_documents({}) == 2  # only people who actually got it became contacts

    # Account topped up → Resume: the 3 who missed it get it, nobody gets it twice.
    db.blast_campaigns.update_one({"_id": ObjectId(bid)}, {"$set": {"status": "sending", "pause_reason": None}})

    def ok(phone, **kw):
        sent.append(phone)
        return {"sid": f"SM{len(sent)}", "status": "queued"}

    _run_blast(db, bid, ok)
    assert len(sent) == 5 and len(set(sent)) == 5
    assert db.blast_campaigns.find_one({"_id": ObjectId(bid)})["status"] == "completed"
    assert db.messages.count_documents({"blast_id": bid}) == 5


def test_a_bad_number_still_fails_only_that_number():
    db, bid = _blast_db(3)

    def one_bad(phone, **kw):
        if phone.endswith("101"):
            raise RuntimeError("Twilio error 63024: Invalid message recipient")
        return {"sid": "SM" + phone[-3:], "status": "queued"}

    _run_blast(db, bid, one_bad)
    assert sorted(r["status"] for r in db.blast_recipients.find({"blast_id": bid})) == ["failed", "sent", "sent"]
    assert db.blast_campaigns.find_one({"_id": ObjectId(bid)})["status"] != "paused"


# ── Blast routes: Retry failed / Resume ────────────────────────────────────────────────────

async def _route_db(status="partially_completed", recipients=()):
    db = AsyncMongoMockClient().db
    bid = (await db.blast_campaigns.insert_one({"user_id": UID, "name": "Meetup invite", "status": status,
                                                "pause_reason": "Twilio rejected the account"})).inserted_id
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    for st, err in recipients:
        await db.blast_recipients.insert_one({"blast_id": str(bid), "user_id": UID, "phone": f"+44{ObjectId()}"[:14],
                                              "status": st, "error": err, "attempt_count": 1, "updated_at": old})
    return db, str(bid)


async def _call(db, fn, bid):
    from app.routes import campaigns as r

    enq = MagicMock()
    with patch.object(r, "get_db", return_value=db), patch.object(r, "enqueue", enq), \
         patch.object(r.ws_manager, "push", new=AsyncMock()):
        out = await fn(bid, user={"_id": ObjectId(UID)})
    return out, enq


@pytest.mark.asyncio
async def test_retry_failed_resends_only_failed_and_stuck_numbers():
    from app.routes import campaigns as r
    from app.workers import tasks

    db, bid = await _route_db(recipients=[("failed", "account … not active"), ("failed", "x"), ("sent", None),
                                           ("undelivered", None), ("processing", None)])
    out, enq = await _call(db, r.retry_failed_blast, bid)
    assert out["retried"] == 2 and out["status"] == "sending" and out["pause_reason"] is None
    counts = {}
    async for rec in db.blast_recipients.find({"blast_id": bid}):
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    assert counts == {"pending": 3, "sent": 1, "undelivered": 1}  # 2 failed + 1 stuck → pending; delivered ones untouched
    assert enq.call_args.args[0] is tasks.send_blast_messages


@pytest.mark.asyncio
async def test_retry_failed_needs_something_to_retry_and_never_runs_on_cancelled():
    from app.routes import campaigns as r

    db, bid = await _route_db(recipients=[("sent", None)])
    with pytest.raises(HTTPException) as e:
        await _call(db, r.retry_failed_blast, bid)
    assert e.value.status_code == 400
    db2, bid2 = await _route_db(status="cancelled", recipients=[("failed", "x")])
    with pytest.raises(HTTPException):
        await _call(db2, r.retry_failed_blast, bid2)


@pytest.mark.asyncio
async def test_resume_clears_the_pause_reason():
    from app.routes import campaigns as r

    db, bid = await _route_db(status="paused", recipients=[("pending", None)])
    out, enq = await _call(db, r.resume_blast, bid)
    assert out["status"] == "sending" and out.get("pause_reason") is None and enq.called


# ── Campaign: same account problem pauses the campaign, contact stays queued ──────────────

def test_campaign_pauses_on_account_problem_and_keeps_the_contact_queued():
    from app.workers import campaign_tasks as ct

    db = mongomock.MongoClient().db
    now = datetime.now(timezone.utc)
    db.users.insert_one({"_id": ObjectId(UID), "email": "t@example.com", "plan": "business", "subscription_status": "active"})
    lead_id = db.leads.insert_one({"user_id": UID, "phone": "+447887190718", "name": "Timiya", "whatsapp_consent_status": "opted_in",
                                   "last_inbound_at": now, "blacklisted": False}).inserted_id
    cid = db.campaigns.insert_one({"user_id": UID, "name": "meetup", "status": "running", "content_mode": "template",
                                   "provider": "twilio", "content_sid": SID, "content_variables": {}, "message": None,
                                   "message_purpose": "campaign", "delivery_scope": "open_window_only", "created_at": now}).inserted_id
    rid = db.campaign_recipients.insert_one({"campaign_id": str(cid), "user_id": UID, "lead_id": str(lead_id), "phone": "+447887190718",
                                             "name": "Timiya", "status": "queued", "attempt_count": 0, "message_purpose": "campaign",
                                             "created_at": now}).inserted_id
    with patch.object(ct, "_db", return_value=db), \
         patch.object(ct.twilio_service, "send_whatsapp", side_effect=_inactive()), \
         patch.object(ct.twilio_service, "get_content_template_info", return_value={"body": "Hi there", "whatsapp_status": "approved"}), \
         patch.object(ct, "_publish"), \
         patch.object(ct, "acquire_send_permit", return_value=True, create=True):
        ct.send_campaign_recipient(UID, str(cid), str(rid))
    rec = db.campaign_recipients.find_one({"_id": rid})
    camp = db.campaigns.find_one({"_id": cid})
    assert rec["status"] == "queued" and rec["attempt_count"] == 0
    assert camp["status"] == "paused" and "Twilio rejected the account" in camp["pause_reason"]
    assert db.notifications.count_documents({"title": "Campaign paused — WhatsApp account problem"}) == 1
