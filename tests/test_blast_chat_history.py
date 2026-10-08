"""Blasts are saved in the contact's chat, so Live Chat shows them and the AI knows what a reply
is answering; delivery updates still reach the blast's own stats."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import mongomock
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

UID = str(ObjectId())
SID = "HX0d84a7134a6deec8ab6176801d04e9c3"
BODY = "Hi {{1}}! 👋 Welcome to AI Testing Hub. I can help you with:\n✅ Course syllabus and modules\n✅ Fees and enrolment"


@pytest.fixture(autouse=True)
def fresh_template_cache():
    from app.services import ai_campaign

    ai_campaign._TEMPLATE_BODY_CACHE.clear()
    yield
    ai_campaign._TEMPLATE_BODY_CACHE.clear()


def _blast(db, *, phone="+447453289655", with_lead=True, variables=None, body=BODY):
    from app.workers import tasks

    now = datetime.now(timezone.utc)
    if not db.users.find_one({"_id": ObjectId(UID)}):
        db.users.insert_one({"_id": ObjectId(UID), "email": "t@example.com", "plan": "business", "subscription_status": "active"})
    if with_lead:
        db.leads.insert_one({"user_id": UID, "phone": phone, "name": "Sriram Angajala", "whatsapp_consent_status": "opted_in",
                             "last_inbound_at": now, "blacklisted": False})
    blast = {"_id": ObjectId(), "user_id": UID, "name": "AI_testing_training", "provider": "twilio", "content_sid": SID,
             "content_variables": variables, "message_purpose": "conversational", "status": "running"}
    db.blast_campaigns.insert_one(blast)
    rid = db.blast_recipients.insert_one({"blast_id": str(blast["_id"]), "user_id": UID, "phone": phone, "status": "pending",
                                          "attempt_count": 0, "created_at": now}).inserted_id
    with patch.object(tasks.twilio_service, "send_whatsapp", return_value={"sid": "SMblast0001", "status": "queued"}), \
         patch.object(tasks.twilio_service, "get_content_template_info", return_value={"body": body, "whatsapp_status": "approved"}), \
         patch.object(tasks, "acquire_send_permit", return_value=True), patch.object(tasks, "_publish"):
        tasks._process_blast_recipient(db, UID, db.blast_recipients.find_one({"_id": rid}), blast=blast, body=None, media_url=None,
                                       content_sid=SID, content_variables=variables, purpose="conversational")
    return blast, db.blast_recipients.find_one({"_id": rid})


def test_blast_is_saved_in_the_contacts_chat_with_the_real_text():
    db = mongomock.MongoClient().db
    blast, rec = _blast(db, variables={"1": ""})
    lead = db.leads.find_one({"phone": "+447453289655"})
    msg = db.messages.find_one({"lead_id": str(lead["_id"])})
    assert rec["status"] == "sent"
    assert msg["direction"] == "outbound" and msg["blast_id"] == str(blast["_id"]) and msg["twilio_sid"] == "SMblast0001"
    assert msg["message"].startswith("Hi Sriram! 👋 Welcome to AI Testing Hub.") and "Fees and enrolment" in msg["message"]


def test_a_number_that_is_not_a_contact_becomes_one_with_the_blast_in_its_chat():
    db = mongomock.MongoClient().db
    blast, rec = _blast(db, with_lead=False, variables={"1": "All"})
    lead = db.leads.find_one({"phone": "+447453289655"})
    assert rec["status"] == "sent" and lead is not None
    assert lead["user_id"] == UID and lead["source"] == "blast" and lead["name"] == "+447453289655"
    assert lead["whatsapp_consent_status"] == "unknown" and lead["ai_paused"] is False  # never auto opted-in
    msg = db.messages.find_one({"lead_id": str(lead["_id"])})
    assert msg["blast_id"] == str(blast["_id"]) and msg["message"].startswith("Hi All! 👋 Welcome to AI Testing Hub.")


def test_blasting_the_same_new_number_twice_keeps_one_contact():
    db = mongomock.MongoClient().db
    _blast(db, with_lead=False, variables={"1": "All"})
    _blast(db, with_lead=False, variables={"1": "All"})
    assert db.leads.count_documents({"phone": "+447453289655"}) == 1
    lead = db.leads.find_one({"phone": "+447453289655"})
    assert db.messages.count_documents({"lead_id": str(lead["_id"])}) == 2


def test_a_failed_send_saves_nothing():
    db = mongomock.MongoClient().db
    _, rec = _blast(db, variables={"1": ""}, body="Your appointment is on {{1}}.")
    assert rec["status"] == "failed" and db.messages.count_documents({}) == 0


def test_a_failed_send_to_a_new_number_creates_no_contact():
    db = mongomock.MongoClient().db
    _, rec = _blast(db, with_lead=False, variables={"1": ""}, body="Your appointment is on {{1}}.")
    assert rec["status"] == "failed" and db.leads.count_documents({}) == 0 and db.messages.count_documents({}) == 0


@pytest.mark.asyncio
async def test_first_reply_names_a_blast_contact_from_their_whatsapp_profile():
    from app.services import inbound_whatsapp
    from app.services.inbound_whatsapp import InboundMessage, process_inbound_message

    db = AsyncMongoMockClient().db
    await db.users.insert_one({"_id": ObjectId(UID), "email": "t@example.com", "twilio_whatsapp_to": "+447828730643"})
    await db.leads.insert_one({"user_id": UID, "phone": "+447453289655", "name": "+447453289655", "source": "blast",
                               "whatsapp_consent_status": "unknown", "blacklisted": False})
    await db.leads.insert_one({"user_id": UID, "phone": "+447700900999", "name": "Priya", "blacklisted": False})
    with patch("app.services.lead_service.get_db", return_value=db), \
         patch.object(inbound_whatsapp.ws_manager, "push", new=AsyncMock()):
        for phone, sid in (("+447453289655", "SMr1"), ("+447700900999", "SMr2")):
            await process_inbound_message(InboundMessage(
                provider="twilio", provider_message_id=sid, customer_phone=phone, business_identifier="+447828730643",
                body="yes I'll come", profile_name="Sriram A", timestamp=None, message_type="text",
                skip_provider_outbound=True, skip_ai_jobs=True), db=db)
    assert (await db.leads.find_one({"phone": "+447453289655"}))["name"] == "Sriram A"
    assert (await db.leads.find_one({"phone": "+447700900999"}))["name"] == "Priya"  # a real name is never overwritten


@pytest.mark.asyncio
async def test_delivery_update_reaches_both_the_chat_message_and_the_blast():
    from app.services import status_callback

    db = AsyncMongoMockClient().db
    bid = (await db.blast_campaigns.insert_one({"user_id": UID, "name": "b", "status": "completed", "total_recipients": 1,
                                                "sent_count": 1})).inserted_id
    await db.blast_recipients.insert_one({"blast_id": str(bid), "user_id": UID, "phone": "+447453289655", "status": "sent",
                                          "twilio_sid": "SMboth"})
    await db.messages.insert_one({"user_id": UID, "lead_id": "l1", "direction": "outbound", "status": "sent",
                                  "twilio_sid": "SMboth", "blast_id": str(bid), "message": "Hi!"})
    with patch.object(status_callback, "get_db", return_value=db), \
         patch.object(status_callback, "_publish_best_effort"), \
         patch("app.services.ws_manager.ws_manager.push", new=AsyncMock()):
        await status_callback.apply_twilio_status_callback(twilio_sid="SMboth", status_raw="read")
    assert (await db.messages.find_one({"twilio_sid": "SMboth"}))["status"] == "read"
    assert (await db.blast_recipients.find_one({"twilio_sid": "SMboth"}))["status"] == "read"


def test_a_late_reply_keeps_the_blast_in_context_and_it_is_labelled():
    from app.services.agent_router import conversation_turns
    from app.services.ai_context import is_business_initiated, load_conversation_context

    db = mongomock.MongoClient().db
    now = datetime.now(timezone.utc)
    db.messages.insert_one({"user_id": UID, "lead_id": "l1", "direction": "outbound", "message": "Hi! Welcome to AI Testing Hub…",
                            "status": "read", "blast_id": "b1", "message_purpose": "conversational", "sender_type": "human",
                            "created_at": now - timedelta(days=2)})
    db.messages.insert_one({"user_id": UID, "lead_id": "l1", "direction": "inbound", "message": "Course syllabus and modules",
                            "status": "received", "created_at": now})
    assert is_business_initiated(db.messages.find_one({"blast_id": "b1"}))
    ctx = load_conversation_context(db, tenant_id=UID, lead_id="l1", session_gap_hours=24)
    assert [m["role"] for m in ctx["messages"]] == ["assistant", "user"]
    assert ctx["messages"][0]["content"].startswith("Hi! Welcome to AI Testing Hub")
    turns = conversation_turns(db, UID, "l1")
    assert turns[0]["who"] == "Broadcast message from the business"


def test_a_contact_named_by_their_number_is_not_greeted_by_it():
    from app.services.ai_prompt import _first_name

    assert _first_name({"name": "+447453289655"}) == ""
    assert _first_name({"name": "447453289655"}) == ""
    assert _first_name({"name": "Sriram Angajala"}) == "Sriram"
