"""Missed-reply recovery: nobody is left unanswered after AI/server outages or forgotten pauses."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import mongomock
import pytest
from bson import ObjectId

from app.config import settings
from app.services import reply_recovery as rr

UID = str(ObjectId())
NOW = datetime.now(timezone.utc)


@pytest.fixture
def db():
    d = mongomock.MongoClient().db
    d.users.insert_one({"_id": ObjectId(UID), "email": "t@example.com", "ai_settings": {"enabled": True}})
    return d


@pytest.fixture
def env(db, monkeypatch):
    """scan_missed_replies against an in-memory DB; AI healthy unless a test says otherwise."""
    state = {"health": (True, None), "jobs": []}
    monkeypatch.setattr(rr, "_db", lambda: db)
    monkeypatch.setattr(rr, "ai_health", lambda **k: state["health"])
    monkeypatch.setattr("app.services.ai_quota.check_quota", lambda tenant_id: (True, None))
    monkeypatch.setattr("app.workers.tasks._publish", lambda *a, **k: None)

    def enqueue(func, *args, **kwargs):
        state["jobs"].append({"func": func.__name__, "args": args, **kwargs})

    state["scan"] = lambda: rr.scan_missed_replies(enqueue_fn=enqueue)
    return state


def _lead(db, **kw):
    doc = {"user_id": UID, "phone": "+447700900111", "name": "Sriram Angajala", "whatsapp_consent_status": "opted_in", **kw}
    return str(db.leads.insert_one(doc).inserted_id)


def _msg(db, lid, direction, text, *, ago, **kw):
    return db.messages.insert_one({"user_id": UID, "lead_id": lid, "direction": direction, "message": text,
                                   "status": "received" if direction == "inbound" else "delivered",
                                   "created_at": NOW - ago, "provider": "twilio", **kw}).inserted_id


# ── what counts as unanswered ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("after, unanswered", [
    (None, True),                                                        # nothing after the customer's message
    ({"message": "Here's the syllabus…", "sender_type": "ai"}, False),   # real AI reply
    ({"message": "On it — Ravi here", "sender_type": "human"}, False),   # a person replied
    ({"message": "Thanks — a team member will follow up", "is_fallback": True}, True),  # holding message only
    ({"message": "Here's the syllabus…", "status": "failed"}, True),     # reply never delivered
    ({"message": "Hi! Welcome…", "blast_id": "b1"}, True),               # a blast isn't an answer
    ({"message": "Join our course…", "campaign_id": "c1"}, True),        # nor is a campaign
    ({"message": "Welcome to AI Testing Hub", "auto_welcome": True}, True),  # nor the automatic welcome
])
def test_what_counts_as_unanswered(db, after, unanswered):
    lid = _lead(db)
    _msg(db, lid, "inbound", "Course syllabus and modules", ago=timedelta(minutes=10))
    if after:
        _msg(db, lid, "outbound", after.pop("message"), ago=timedelta(minutes=9), **after)
    assert bool(rr.find_unanswered(db, now=NOW)) is unanswered


def test_a_message_younger_than_the_wait_is_not_unanswered_yet(db):
    lid = _lead(db)
    _msg(db, lid, "inbound", "hi", ago=timedelta(seconds=60))
    assert rr.find_unanswered(db, now=NOW) == []


# ── recovery decisions ─────────────────────────────────────────────────────────────────

def test_ai_healthy_sends_one_catch_up_reply(db, env):
    lid = _lead(db)
    trig = _msg(db, lid, "inbound", "Course syllabus and modules", ago=timedelta(minutes=17))
    s = env["scan"]()
    assert s["replying"] == 1 and len(env["jobs"]) == 1
    job = env["jobs"][0]
    assert job["func"] == "generate_and_send_ai_reply" and job["args"] == (UID, lid)
    assert job["trigger_message_id"] == str(trig) and job["catch_up_minutes"] == 17 and job["queue"] == "high"
    state = db.leads.find_one({"_id": ObjectId(lid)})["missed_reply"]
    assert state["reason"] == "replying" and state["attempts"] == 1
    env["scan"]()  # a second scan minutes later doesn't queue another reply
    assert len(env["jobs"]) == 1


def test_ai_down_waits_and_alerts_once(db, env):
    env["health"] = (False, "authentication")
    for i in range(2):
        lid = _lead(db, phone=f"+44770090020{i}")
        _msg(db, lid, "inbound", "FEES", ago=timedelta(minutes=8))
    env["scan"](); env["scan"]()
    assert env["jobs"] == []
    reasons = {d["missed_reply"]["reason"] for d in db.leads.find({"missed_reply": {"$exists": True}})}
    assert reasons == {"ai_down:authentication"}
    alerts = list(db.notifications.find({"title": "AI replies are down"}))
    assert len(alerts) == 1 and "2 customer(s) waiting" in alerts[0]["message"] and "authentication" in alerts[0]["message"]
    env["health"] = (True, None)  # key fixed → everyone gets their catch-up
    env["scan"]()
    assert len(env["jobs"]) == 2


def test_paused_chat_waits_then_ai_takes_over(db, env, monkeypatch):
    monkeypatch.setattr(settings, "AI_AUTO_HANDBACK_HOURS", 2.0)
    lid = _lead(db, ai_paused=True)
    _msg(db, lid, "inbound", "hi", ago=timedelta(minutes=30))
    env["scan"]()
    assert env["jobs"] == [] and db.leads.find_one({"_id": ObjectId(lid)})["missed_reply"]["reason"] == "ai_paused"
    db.messages.update_many({"lead_id": lid}, {"$set": {"created_at": NOW - timedelta(hours=3)}})
    env["scan"]()
    lead = db.leads.find_one({"_id": ObjectId(lid)})
    assert lead["ai_paused"] is False and len(env["jobs"]) == 1
    assert db.activity_events.find_one({"resource_id": lid})["event_type"] == "lead.ai_auto_resume"


def test_takeover_with_handback_off_stays_with_the_team(db, env, monkeypatch):
    monkeypatch.setattr(settings, "AI_AUTO_HANDBACK_HOURS", 0)
    lid = _lead(db, takeover_by="someone")
    _msg(db, lid, "inbound", "hello?", ago=timedelta(hours=5))
    env["scan"]()
    assert env["jobs"] == [] and db.leads.find_one({"_id": ObjectId(lid)})["missed_reply"]["reason"] == "human_takeover"


def test_past_the_whatsapp_window_goes_to_a_person(db, env):
    lid = _lead(db)
    _msg(db, lid, "inbound", "hi", ago=timedelta(hours=30))
    env["scan"]()
    lead = db.leads.find_one({"_id": ObjectId(lid)})
    assert env["jobs"] == [] and lead["needs_human"] and lead["needs_human_reason"] == "missed_window_closed"
    assert db.notifications.find_one({"title": "A customer waited too long for a reply"})


def test_gives_up_after_max_attempts_and_asks_a_person(db, env, monkeypatch):
    monkeypatch.setattr(settings, "RECOVERY_MAX_ATTEMPTS", 2)
    lid = _lead(db)
    trig = _msg(db, lid, "inbound", "hi", ago=timedelta(minutes=40))
    db.leads.update_one({"_id": ObjectId(lid)}, {"$set": {"missed_reply": {"trigger_id": str(trig), "attempts": 2,
                                                                           "last_attempt_at": NOW - timedelta(minutes=10)}}})
    env["scan"]()
    lead = db.leads.find_one({"_id": ObjectId(lid)})
    assert env["jobs"] == [] and lead["needs_human_reason"] == "missed_reply_failed"


def test_answered_chats_are_cleared(db, env):
    lid = _lead(db)
    _msg(db, lid, "inbound", "hi", ago=timedelta(minutes=10))
    env["scan"]()
    _msg(db, lid, "outbound", "Sorry for the slow reply! Hi…", ago=timedelta(minutes=1), sender_type="ai")
    s = env["scan"]()
    assert s["cleared"] == 1 and "missed_reply" not in db.leads.find_one({"_id": ObjectId(lid)})


@pytest.mark.parametrize("lead_kw, user_ai", [
    ({"whatsapp_consent_status": "opted_out"}, True),
    ({"blacklisted": True}, True),
    ({}, False),  # tenant switched AI off — they answer by hand
])
def test_opted_out_blacklisted_or_ai_off_are_left_alone(db, env, lead_kw, user_ai):
    db.users.update_one({"_id": ObjectId(UID)}, {"$set": {"ai_settings": {"enabled": user_ai}}})
    lid = _lead(db, **lead_kw)
    _msg(db, lid, "inbound", "hi", ago=timedelta(minutes=10))
    s = env["scan"]()
    assert env["jobs"] == [] and s["waiting"] == 0


# ── AI health probe ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status, body, expected", [
    (200, {}, (True, None)),
    (401, {"error": {"code": "invalid_api_key"}}, (False, "authentication")),
    (429, {"error": {"code": "insufficient_quota"}}, (False, "credits_exhausted")),
    (429, {"error": {"code": "rate_limit_exceeded"}}, (False, "rate_limited")),
    (503, {}, (False, "provider_down")),
])
def test_ai_health_tells_revoked_key_from_no_credits(monkeypatch, status, body, expected):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(rr.httpx, "post", lambda *a, **k: SimpleNamespace(status_code=status, json=lambda: body))
    assert rr.ai_health(refresh=True) == expected


def test_ai_health_is_cached_for_a_minute(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    calls = []
    monkeypatch.setattr(rr.httpx, "post", lambda *a, **k: calls.append(1) or SimpleNamespace(status_code=200, json=lambda: {}))
    rr.ai_health(refresh=True); rr.ai_health(); rr.ai_health()
    assert len(calls) == 1


# ── server down: messages only Twilio saw ─────────────────────────────────────────────

def test_twilio_messages_we_never_received_are_replayed(db, monkeypatch):
    monkeypatch.setattr(rr, "_db", lambda: db)
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC" + "0" * 32)
    db.users.update_one({"_id": ObjectId(UID)}, {"$set": {"twilio_whatsapp_to": "+447828730643"}})
    db.messages.insert_one({"user_id": UID, "lead_id": "l1", "direction": "inbound", "twilio_sid": "SMknown", "created_at": NOW})

    def m(sid, direction, body):
        return SimpleNamespace(sid=sid, direction=direction, from_="whatsapp:+447700900111", to="whatsapp:+447828730643",
                               body=body, num_media="0", date_sent=NOW)

    client = MagicMock()
    client.messages.list.return_value = [m("SMknown", "inbound", "hi"), m("SMmissed", "inbound", "are you there?"),
                                         m("SMout", "outbound-api", "our reply")]
    replayed = []
    out = rr.reconcile_twilio_inbound(client=client, replay_fn=lambda p: replayed.append(p) or True)
    assert out == {"checked": 2, "missing": 1, "replayed": 1}
    assert replayed[0]["MessageSid"] == "SMmissed" and replayed[0]["Body"] == "are you there?"
    assert replayed[0]["From"] == "whatsapp:+447700900111" and replayed[0]["To"] == "whatsapp:+447828730643"
    assert client.messages.list.call_args.kwargs["to"] == "whatsapp:+447828730643"


# ── never two answers; catch-up apologises ────────────────────────────────────────────

def test_duplicate_guards():
    from app.workers.tasks import _answered_after

    d = mongomock.MongoClient().db
    t = NOW - timedelta(minutes=20)
    d.messages.insert_one({"lead_id": "l1", "direction": "outbound", "status": "sent", "is_fallback": True, "created_at": t + timedelta(seconds=5)})
    assert _answered_after(d, "l1", t) is False                      # holding message ≠ answer → catch-up proceeds
    d.messages.insert_one({"lead_id": "l1", "direction": "outbound", "status": "sent", "catch_up_for": "trig1", "created_at": t + timedelta(minutes=15)})
    assert _answered_after(d, "l1", t) is True                       # answered → a second catch-up skips
    assert _answered_after(d, "l1", t, catch_up_for="trig1") is True  # late original job skips too
    assert _answered_after(d, "l1", t, catch_up_for="other") is False


def test_catch_up_prompt_apologises_for_the_wait():
    from app.services.ai_prompt import build_system_prompt, format_delay_note

    assert format_delay_note(None) == "" and format_delay_note(0) == ""
    assert "waiting 17 minutes" in format_delay_note(17) and "apology" in format_delay_note(17)
    assert "about 3 hours" in format_delay_note(180)
    assert format_delay_note(17) in build_system_prompt(agent={"name": "A"}, delayed_minutes=17)
    assert format_delay_note(17) in build_system_prompt(neutral=True, delayed_minutes=17)
