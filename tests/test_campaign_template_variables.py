"""Template campaigns ("No agent — use template") with a blank variable, through the real sender.

Twilio rejects empty template variables ("The Content Variables parameter is invalid"). A blank
variable the template greets gets each contact's first name; any other blank fails the recipient
with a clear message, without calling Twilio and without retrying.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import mongomock
import pytest
from bson import ObjectId

from app.workers import campaign_tasks as ct

UID = str(ObjectId())
SID = "HX9aebf35fbf8735b67323a23666899d25"
GREETING_BODY = "Hi {{1}}, exam season is approaching! Reply to this message to get started."


@pytest.fixture
def db():
    return mongomock.MongoClient().db


def _setup(db, *, variables, lead_name="Timiya Boniface"):
    now = datetime.now(timezone.utc)
    db.users.insert_one({"_id": ObjectId(UID), "email": "t@example.com", "plan": "business",
                         "subscription_status": "active"})
    lead_id = db.leads.insert_one({"user_id": UID, "phone": "+447887190718", "name": lead_name,
                                   "whatsapp_consent_status": "opted_in", "last_inbound_at": now,
                                   "blacklisted": False}).inserted_id
    cid = db.campaigns.insert_one({"user_id": UID, "name": "exam revision", "status": "running",
                                   "content_mode": "template", "provider": "twilio", "content_sid": SID,
                                   "template_id": None, "content_variables": variables, "message": None,
                                   "message_purpose": "campaign", "delivery_scope": "open_window_only",
                                   "created_at": now}).inserted_id
    rid = db.campaign_recipients.insert_one({"campaign_id": str(cid), "user_id": UID, "lead_id": str(lead_id),
                                             "phone": "+447887190718", "name": lead_name, "status": "queued",
                                             "attempt_count": 0, "message_purpose": "campaign",
                                             "created_at": now}).inserted_id
    return str(cid), str(rid)


def _send(db, cid, rid, body=GREETING_BODY):
    sent = []

    def fake_send(phone, **kw):
        sent.append({"phone": phone, **kw})
        return {"sid": "SMfake0001", "status": "queued", "provider_message_id": "SMfake0001"}

    with patch.object(ct, "_db", return_value=db), \
         patch.object(ct.twilio_service, "send_whatsapp", side_effect=fake_send), \
         patch.object(ct.twilio_service, "get_content_template_info",
                      return_value={"body": body, "whatsapp_status": "approved"}), \
         patch.object(ct, "_publish"), \
         patch.object(ct, "acquire_send_permit", return_value=True, create=True):
        ct.send_campaign_recipient(UID, cid, rid)
    return sent, db.campaign_recipients.find_one({"_id": ObjectId(rid)})


def test_blank_name_variable_is_sent_as_the_contacts_first_name(db):
    cid, rid = _setup(db, variables={"1": ""})
    sent, rec = _send(db, cid, rid)
    assert len(sent) == 1 and sent[0]["content_sid"] == SID
    assert sent[0]["content_variables"] == {"1": "Timiya"}
    assert rec["status"] == "sent" and not rec.get("error_message")


def test_typed_variable_is_sent_unchanged(db):
    cid, rid = _setup(db, variables={"1": "Timiya"}, lead_name="Someone Else")
    sent, rec = _send(db, cid, rid)
    assert sent[0]["content_variables"] == {"1": "Timiya"} and rec["status"] == "sent"


def test_blank_non_greeting_variable_fails_clearly_without_calling_twilio(db):
    cid, rid = _setup(db, variables={"1": ""})
    sent, rec = _send(db, cid, rid, body="Your appointment is on {{1}}. Reply to confirm.")
    assert sent == []
    assert rec["status"] == "failed"  # not "retrying" — retrying can't fix a blank variable
    assert "Template variable {{1}} is empty" in rec["error_message"]
