"""Live lookups: web search for current information, its guard rails, and how replies use it."""
import mongomock
import pytest

from app.config import settings
from app.services import web_lookup
from app.services.ai_prompt import build_system_prompt, current_time_line, format_live_lookup
from app.services.web_lookup import LookupResult, live_lookup

RESPONSE = {
    "output": [
        {"type": "web_search_call", "status": "completed"},
        {"type": "message", "content": [{
            "type": "output_text",
            "text": "Dagenham today: light rain, 14°C, clearing later.",
            "annotations": [
                {"type": "url_citation", "url": "https://weather.metoffice.gov.uk/x?utm_source=openai"},
                {"type": "url_citation", "url": "https://weather.metoffice.gov.uk/x?utm_source=openai"},
            ],
        }]},
    ],
    "usage": {"input_tokens": 8000, "output_tokens": 60},
}


@pytest.fixture
def posted(monkeypatch):
    calls, recorded = [], []

    def fake_post(body):
        calls.append(body)
        return RESPONSE

    monkeypatch.setattr(web_lookup, "_post", fake_post)
    monkeypatch.setattr(web_lookup, "check_quota", lambda tenant_id: (True, None))
    monkeypatch.setattr(web_lookup, "_record", lambda *a: recorded.append(a))
    return {"calls": calls, "recorded": recorded}


def test_lookup_returns_answer_with_clean_unique_sources(posted):
    res = live_lookup("weather in Dagenham today", tenant_id="t1")
    assert res.ok and "14°C" in res.text and res.sources == ["https://weather.metoffice.gov.uk/x"]
    body = posted["calls"][0]
    assert body["tools"][0]["type"] == "web_search" and body["tools"][0]["user_location"]["country"] == "GB"
    assert body["model"] == settings.AI_WEB_LOOKUP_MODEL
    assert posted["recorded"] == [("t1", 8000, 60, posted["recorded"][0][3], 1)]


def test_same_question_is_served_from_cache(posted):
    live_lookup("weather in Dagenham today", tenant_id="t1")
    again = live_lookup("Weather in  Dagenham today", tenant_id="t2")
    assert again.ok and again.cached and len(posted["calls"]) == 1


def test_daily_cap_per_tenant(posted, monkeypatch):
    monkeypatch.setattr(settings, "AI_WEB_LOOKUP_DAILY_LIMIT_PER_TENANT", 2)
    assert live_lookup("q one", tenant_id="t1").ok
    assert live_lookup("q two", tenant_id="t1").ok
    over = live_lookup("q three", tenant_id="t1")
    assert not over.ok and over.reason == "over_daily_limit" and len(posted["calls"]) == 2
    assert live_lookup("q three", tenant_id="t2").ok  # other tenants unaffected


def test_quota_exceeded_skips_the_search(posted, monkeypatch):
    monkeypatch.setattr(web_lookup, "check_quota", lambda tenant_id: (False, "daily_tokens"))
    res = live_lookup("news today", tenant_id="t1")
    assert not res.ok and res.reason == "quota_exceeded" and posted["calls"] == []


def test_kill_switch(posted, monkeypatch):
    monkeypatch.setattr(settings, "AI_WEB_LOOKUP_ENABLED", False)
    res = live_lookup("news today", tenant_id="t1")
    assert not res.ok and res.reason == "disabled" and posted["calls"] == []


def test_network_error_never_raises(monkeypatch):
    monkeypatch.setattr(web_lookup, "check_quota", lambda tenant_id: (True, None))
    res = live_lookup("weather", tenant_id="t1")  # conftest blocks the real call → raises inside
    assert not res.ok and res.reason == "error"


def test_empty_answer_is_not_cached(posted, monkeypatch):
    monkeypatch.setattr(web_lookup, "_post", lambda body: posted["calls"].append(body) or {"output": []})
    assert live_lookup("odd query", tenant_id="t1").reason == "empty"
    assert live_lookup("odd query", tenant_id="t1").reason == "empty" and len(posted["calls"]) == 2


def test_reply_sub_steps_do_not_use_up_the_per_minute_request_limit(monkeypatch):
    from app.services import ai_quota

    monkeypatch.setattr(settings, "AI_MAX_REQUESTS_PER_MINUTE_PER_TENANT", 3)
    db = mongomock.MongoClient().db
    for _ in range(4):  # one busy minute: four customer messages, each with its helper calls
        for op in ("agent_route", "kb_query", "web_lookup"):
            ai_quota.record_usage_sync(db, tenant_id="t1", operation=op, model="gpt-4o-mini", input_tokens=10)
    assert ai_quota.check_quota("t1") == (True, None)
    for _ in range(3):
        ai_quota.record_usage_sync(db, tenant_id="t1", operation="reply", model="gpt-4o-mini", input_tokens=10)
    assert ai_quota.check_quota("t1") == (False, "quota_exceeded")  # replies still count


def test_search_fee_counts_toward_ai_cost(monkeypatch):
    from app.services import ai_quota

    monkeypatch.setattr(ai_quota, "record_quota_usage", lambda *a, **k: None)
    db = mongomock.MongoClient().db
    plain = ai_quota.record_usage_sync(db, tenant_id="t1", operation="x", model="gpt-4o-mini", input_tokens=1000)
    fee = ai_quota.record_usage_sync(db, tenant_id="t1", operation="web_lookup", model="gpt-4o-mini",
                                     input_tokens=1000, extra_cost=0.01)
    assert fee["estimated_cost"] == pytest.approx(plain["estimated_cost"] + 0.01)


# ── prompts ──────────────────────────────────────────────────────────────────

def test_time_line_uses_uk_time_when_tenant_timezone_is_utc_or_missing():
    assert current_time_line("UTC").endswith("(Europe/London).")
    assert current_time_line(None).endswith("(Europe/London).")
    assert current_time_line("Asia/Kolkata").endswith("(Asia/Kolkata).")
    assert current_time_line("Not/AZone").endswith("(Europe/London).")


def test_every_prompt_has_the_date_and_the_live_info_rule():
    agent = {"name": "AI Testing Hub Agent", "business_description": "AI testing"}
    for p in (build_system_prompt(agent=agent), build_system_prompt(neutral=True)):
        assert "Current date and time:" in p and "Never guess or invent current conditions" in p


def test_lookup_results_reach_general_and_agent_prompts():
    res = LookupResult(query="weather in Dagenham today", ok=True, text="Light rain, 14°C.",
                       sources=["https://weather.metoffice.gov.uk/x"])
    for p in (build_system_prompt(neutral=True, live_lookup=res),
              build_system_prompt(agent={"name": "A"}, live_lookup=res)):
        assert 'LIVE LOOKUP RESULTS (web search just now for "weather in Dagenham today")' in p
        assert "Light rain, 14°C." in p and "metoffice" in p


def test_failed_lookup_tells_the_reply_to_say_so():
    block = format_live_lookup(LookupResult(query="weather today", ok=False, reason="error"))
    assert "unavailable right now" in block and "Never guess" in block
    assert format_live_lookup(None) == ""


def test_live_reminder_follows_the_knowledge_reminder(monkeypatch):
    from app.services import openai_service
    from app.services.kb.retrieve import KBContext

    seen = {}

    class R:
        success, text = True, "It's 14°C and rainy in Dagenham."

    def fake(messages, **kw):
        seen["messages"] = messages
        return R()

    monkeypatch.setattr(openai_service, "chat_completion", fake)
    ai = {"enabled": True, "temperature": 0.5, "model": "m", "fallback_model": "m", "max_output_tokens": 200}
    live = LookupResult(query="weather", ok=True, text="14°C rain")
    openai_service.generate_reply({"name": "A"}, [{"role": "user", "content": "weather?"}], ai_settings=ai,
                                  kb_context=KBContext(kb_ids=["k"], searched=True, hits=[]), live_lookup=live)
    tail = [m["content"] for m in seen["messages"][-2:]]
    assert tail == [openai_service.NO_MATCH_REMINDER, openai_service.LIVE_REMINDER]


def test_answered_by_lookup_is_not_a_knowledge_gap():
    from app.services.kb.retrieve import KBContext
    from app.workers.tasks import _needs_team_followup

    gap = KBContext(kb_ids=["k"], searched=True, hits=[])
    live = LookupResult(query="weather", ok=True, text="14°C")
    assert _needs_team_followup(gap, "It's 14°C and rainy.", live) is False
    assert _needs_team_followup(gap, "I'll check with the team and get back to you.", live) is True
    # lookup unavailable → "I can't check right now" isn't a hand-off either
    assert _needs_team_followup(gap, "I can't check the weather right now.", LookupResult(query="w", ok=False)) is False
    assert _needs_team_followup(gap, "Sure!") is True  # no lookup: a knowledge miss still flags
