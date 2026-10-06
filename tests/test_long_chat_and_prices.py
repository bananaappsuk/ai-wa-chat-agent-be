"""Long chats keep the first customer message available; price checks read "£1,000" correctly."""
from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from app.services.ai_context import load_conversation_context
from app.services.ai_prompt import build_system_prompt, format_first_message
from app.services.ai_quality import validate_output

UID, LID = "tenant1", "lead1"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def db():
    return mongomock.MongoClient().db


def _chat(db, pairs, *, start):
    t = start
    for user, bot in pairs:
        db.messages.insert_one({"user_id": UID, "lead_id": LID, "direction": "inbound", "message": user, "created_at": t})
        t += timedelta(seconds=20)
        db.messages.insert_one({"user_id": UID, "lead_id": LID, "direction": "outbound", "message": bot,
                                "status": "delivered", "created_at": t})
        t += timedelta(seconds=40)


def test_long_chat_keeps_the_first_customer_message(db):
    _chat(db, [(f"question {i}", f"answer {i}") for i in range(1, 31)], start=NOW - timedelta(hours=1))
    ctx = load_conversation_context(db, tenant_id=UID, lead_id=LID, max_messages=20, session_gap_hours=24)
    assert len(ctx["messages"]) == 20 and ctx["messages"][0]["content"] != "question 1"
    assert ctx["first_customer_message"] == "question 1"


def test_short_chat_needs_no_extra_first_message(db):
    _chat(db, [("hi", "hello"), ("course?", "it covers…")], start=NOW - timedelta(minutes=5))
    ctx = load_conversation_context(db, tenant_id=UID, lead_id=LID, max_messages=20, session_gap_hours=24)
    assert ctx["first_customer_message"] is None


def test_first_message_comes_from_the_current_conversation_not_an_old_one(db):
    _chat(db, [("old chat last week", "old reply")], start=NOW - timedelta(days=7))
    _chat(db, [(f"q{i}", f"a{i}") for i in range(1, 31)], start=NOW - timedelta(hours=1))
    ctx = load_conversation_context(db, tenant_id=UID, lead_id=LID, max_messages=20, session_gap_hours=24)
    assert ctx["first_customer_message"] == "q1"


def test_first_message_reaches_both_prompts():
    line = format_first_message("hi there")
    assert line.startswith("EARLIEST CUSTOMER MESSAGE") and '"hi there"' in line
    assert line in build_system_prompt(neutral=True, first_message="hi there")
    assert line in build_system_prompt(agent={"name": "A"}, first_message="hi there")
    assert "EARLIEST CUSTOMER MESSAGE" not in build_system_prompt(neutral=True)
    assert format_first_message(None) == ""


def test_generate_reply_passes_the_first_message_to_the_model(monkeypatch):
    from app.services import openai_service

    seen = {}

    class R:
        success, text = True, "You first asked about the course."

    def fake(messages, **kw):
        seen["system"] = messages[0]["content"]
        return R()

    monkeypatch.setattr(openai_service, "chat_completion", fake)
    ai = {"enabled": True, "temperature": 0.3, "model": "m", "fallback_model": "m", "max_output_tokens": 200}
    openai_service.generate_reply({"name": "A"}, [{"role": "user", "content": "what did I ask first?"}],
                                  ai_settings=ai, first_message="what is this course about?")
    assert '"what is this course about?"' in seen["system"]


@pytest.mark.parametrize("text, floor, ceiling, ok, reason", [
    ("Pay £1,000 after you get a job.", 499, None, True, None),          # was read as £1 → wrongly blocked
    ("Pay £1,000 after you get a job.", None, 999, False, "price_above_ceiling"),
    ("£1,234.56 in total", 1234, 1235, True, None),
    ("The course is £499.", 499, 499, True, None),
    ("Two payments of £249.50.", 499, None, False, "price_below_floor"),
    ("Only £19 per month.", None, None, True, None),
])
def test_price_bounds_read_thousands_separators(text, floor, ceiling, ok, reason):
    q = validate_output(text, price_floor=floor, price_ceiling=ceiling)
    assert q.ok is ok and q.reason == reason
