"""AI replies only see the current conversation, not weeks-old history."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from app.services.ai_context import current_session, is_business_initiated, load_conversation_context

T0 = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)


def _m(direction, text, at, **extra):
    return {"direction": direction, "message": text, "created_at": at, **extra}


def test_old_session_is_cut_off():
    raw = [
        _m("inbound", "hi", T0),
        _m("inbound", "can you talk about internship", T0 + timedelta(minutes=2)),
        _m("inbound", "Hey hi how are you", T0 + timedelta(days=18)),
    ]
    out = current_session(raw, 24)
    assert [m["message"] for m in out] == ["Hey hi how are you"]


def test_live_conversation_kept_whole():
    raw = [_m("inbound", f"m{i}", T0 + timedelta(hours=3 * i)) for i in range(6)]  # 3h apart
    assert current_session(raw, 24) == raw


def test_late_reply_to_campaign_keeps_the_campaign_message():
    raw = [
        _m("inbound", "old chat", T0),
        _m("outbound", "AI Weekend Workshop this Saturday — interested?", T0 + timedelta(days=5),
           message_purpose="campaign", campaign_id="c1"),
        _m("inbound", "yes tell me more", T0 + timedelta(days=7)),
    ]
    out = current_session(raw, 24)
    assert [m["message"] for m in out] == [
        "AI Weekend Workshop this Saturday — interested?",
        "yes tell me more",
    ]


def test_late_reply_to_an_ai_reply_is_a_new_conversation():
    raw = [
        _m("inbound", "internship?", T0),
        _m("outbound", "We offer internships...", T0 + timedelta(minutes=1), message_purpose="support"),
        _m("inbound", "hello", T0 + timedelta(days=10)),
    ]
    assert [m["message"] for m in current_session(raw, 24)] == ["hello"]


def test_zero_gap_disables_cut():
    raw = [_m("inbound", "a", T0), _m("inbound", "b", T0 + timedelta(days=30))]
    assert current_session(raw, 0) == raw
    assert current_session(raw, None) == raw


def test_naive_datetimes_from_mongo_are_handled():
    naive = T0.replace(tzinfo=None)
    raw = [_m("inbound", "a", naive), _m("inbound", "b", naive + timedelta(days=2))]
    assert [m["message"] for m in current_session(raw, 24)] == ["b"]


def test_is_business_initiated():
    assert is_business_initiated({"direction": "outbound", "message_purpose": "marketing"})
    assert is_business_initiated({"direction": "outbound", "campaign_id": "x"})
    assert not is_business_initiated({"direction": "outbound", "message_purpose": "support"})
    assert not is_business_initiated({"direction": "inbound", "campaign_id": "x"})
    assert not is_business_initiated(None)


def test_load_conversation_context_applies_session_cut():
    docs_newest_first = [
        _m("inbound", "Hey hi how are you", T0 + timedelta(days=18)),
        _m("inbound", "can you talk about internship", T0 + timedelta(minutes=2)),
        _m("inbound", "hi", T0),
    ]
    db = MagicMock()
    db.messages.find.return_value.sort.return_value.limit.return_value = docs_newest_first
    ctx = load_conversation_context(db, tenant_id="t", lead_id="l", session_gap_hours=24)
    assert [m["content"] for m in ctx["messages"]] == ["Hey hi how are you"]
    # without the gap (other callers) behaviour is unchanged
    ctx_all = load_conversation_context(db, tenant_id="t", lead_id="l")
    assert len(ctx_all["messages"]) == 3
