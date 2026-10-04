"""Inbound routing: decided fresh for every message from the conversation — no agent locks."""
from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from app.services import agent_router
from app.services.agent_router import conversation_turns, route_message, select_agent_for_inbound

UID = "tenant1"
NOW = datetime.now(timezone.utc)


@pytest.fixture
def db():
    return mongomock.MongoClient().db


def _agent(db, name, *, keywords=None, description="", is_default=False, status="active"):
    doc = {"user_id": UID, "name": name, "status": status, "routing_keywords": keywords or [],
           "description": description, "is_default": is_default, "updated_at": NOW}
    doc["_id"] = db.agents.insert_one(doc).inserted_id
    return doc


def _lead(db, **kw):
    doc = {"user_id": UID, "phone": "+447700900000", "name": "Priya", **kw}
    doc["_id"] = db.leads.insert_one(doc).inserted_id
    return doc


def _msg(db, lead, direction, text, *, minutes_ago=0, **kw):
    db.messages.insert_one({"user_id": UID, "lead_id": str(lead["_id"]), "direction": direction, "message": text,
                            "status": "delivered" if direction == "outbound" else "received",
                            "created_at": NOW - timedelta(minutes=minutes_ago), **kw})


@pytest.fixture
def router(monkeypatch):
    """Capture what the routing model sees and script what it answers."""
    calls = []
    answer = {"value": {"route": "general", "agent": None, "continues_previous": False, "search_query": ""}}

    def fake(agents, turns, *, tenant_id):
        calls.append({"agents": [a["name"] for a in agents], "turns": turns})
        return answer["value"]

    monkeypatch.setattr(agent_router, "_llm_route", fake)
    return {"calls": calls, "answer": answer}


def test_no_agents_means_general_but_still_checks_for_a_live_lookup(db, router):
    lead = _lead(db)
    router["answer"]["value"] = {"route": "general", "live_lookup": "weather in Dagenham today"}
    d = route_message(db, UID, lead, message_text="weather in dagenham today?")
    assert d.agent is None and d.mode == "none" and d.lookup_query == "weather in Dagenham today"
    assert router["calls"][0]["agents"] == []


def test_greeting_mid_conversation_goes_to_general_not_the_agent(db, router):
    testing = _agent(db, "AI Testing Hub Agent", description="AI testing courses")
    lead = _lead(db, assigned_agent_id=str(testing["_id"]), assigned_agent_source="auto")
    _msg(db, lead, "inbound", "tell me about the course", minutes_ago=5)
    _msg(db, lead, "outbound", "The course covers evaluation...", minutes_ago=4, agent_name="AI Testing Hub Agent", sender_type="ai")
    _msg(db, lead, "inbound", "hey how are you")
    d = route_message(db, UID, lead)
    assert d.agent is None and d.mode == "general"  # previously "locked" agent does not answer
    assert d.topics == ["AI testing courses"]


def test_router_sees_labelled_conversation_and_latest_message_last(db, router):
    _agent(db, "AI Testing Hub Agent", description="AI testing")
    _agent(db, "Restaurant", description="menu and bookings")
    lead = _lead(db)
    _msg(db, lead, "inbound", "what does the course cover?", minutes_ago=3)
    _msg(db, lead, "outbound", "Evaluation, CI/CD and governance.", minutes_ago=2, agent_name="AI Testing Hub Agent", sender_type="ai")
    _msg(db, lead, "inbound", "how long is it?")
    router["answer"]["value"] = {"route": "agent", "agent_name": "AI Testing Hub Agent", "continues_previous": True,
                                 "search_query": "How long is the AI agent testing course?"}
    d = route_message(db, UID, lead)
    turns = router["calls"][0]["turns"]
    assert turns[-1] == {"who": "Customer", "text": "how long is it?"}
    assert {"who": "AI Testing Hub Agent", "text": "Evaluation, CI/CD and governance."} in turns
    assert d.agent["name"] == "AI Testing Hub Agent" and d.continues is True
    assert d.query == "How long is the AI agent testing course?"


def test_chosen_agent_is_recorded_as_last_handler_not_a_lock(db, router):
    a = _agent(db, "Restaurant", description="menu")
    lead = _lead(db)
    _msg(db, lead, "inbound", "what's on the menu?")
    router["answer"]["value"] = {"route": "agent", "agent": 0, "continues_previous": False, "search_query": "restaurant menu"}
    route_message(db, UID, lead)
    stored = db.leads.find_one({"_id": lead["_id"]})
    assert stored["assigned_agent_id"] == str(a["_id"]) and stored["assigned_agent_source"] == "auto"
    # the next message is decided fresh — the record doesn't force the agent
    router["answer"]["value"] = {"route": "general", "agent": None, "continues_previous": False, "search_query": ""}
    _msg(db, lead, "inbound", "thanks!")
    assert route_message(db, UID, db.leads.find_one({"_id": lead["_id"]})).agent is None


def test_manual_pin_always_wins_over_the_model(db, router):
    a = _agent(db, "AI Testing Hub Agent")
    _agent(db, "Restaurant", keywords=["menu"])
    lead = _lead(db, assigned_agent_id=str(a["_id"]), assigned_agent_source="manual")
    _msg(db, lead, "inbound", "what's on the menu?")
    router["answer"]["value"] = {"route": "agent", "agent_name": "Restaurant", "search_query": "Restaurant menu"}
    d = route_message(db, UID, lead)
    assert d.agent["name"] == "AI Testing Hub Agent" and d.mode == "pinned"
    assert d.query == ""  # the model's query names another business — the pinned agent rewrites its own


def test_pinned_agent_uses_the_models_query_and_lookup_when_it_agrees(db, router):
    a = _agent(db, "AI Testing Hub Agent")
    lead = _lead(db, assigned_agent_id=str(a["_id"]), assigned_agent_source="manual")
    _msg(db, lead, "inbound", "is the meetup still on with this weather?")
    router["answer"]["value"] = {"route": "agent", "agent_name": "AI Testing Hub Agent",
                                 "search_query": "AI Testing Hub meetup date", "live_lookup": "weather in London today"}
    d = route_message(db, UID, lead)
    assert d.mode == "pinned" and d.query == "AI Testing Hub meetup date" and d.lookup_query == "weather in London today"


def test_live_lookup_query_is_passed_through_for_general_and_agent_routes(db, router):
    _agent(db, "AI Testing Hub Agent", description="AI testing courses")
    lead = _lead(db)
    _msg(db, lead, "inbound", "whats the weather in london today?")
    router["answer"]["value"] = {"route": "general", "live_lookup": "weather in London today"}
    d = route_message(db, UID, lead)
    assert d.agent is None and d.lookup_query == "weather in London today"
    router["answer"]["value"] = {"route": "agent", "agent_name": "AI Testing Hub Agent", "search_query": "course"}
    _msg(db, lead, "inbound", "and the course?")
    assert route_message(db, UID, lead).lookup_query == ""


def test_no_lookup_when_the_routing_model_is_unavailable(db, monkeypatch):
    monkeypatch.setattr(agent_router, "_llm_route", lambda *a, **k: None)
    lead = _lead(db)
    _msg(db, lead, "inbound", "weather today?")
    assert route_message(db, UID, lead).lookup_query == ""


def test_manual_pin_is_not_overwritten_when_pinned_agent_is_inactive(db, router):
    a = _agent(db, "Old agent", status="inactive")
    r = _agent(db, "Restaurant")
    lead = _lead(db, assigned_agent_id=str(a["_id"]), assigned_agent_source="manual")
    _msg(db, lead, "inbound", "menu?")
    router["answer"]["value"] = {"route": "agent", "agent": 0, "continues_previous": False, "search_query": "menu"}
    d = route_message(db, UID, lead)
    assert d.agent["_id"] == r["_id"]  # routed normally while the pinned agent is off
    assert db.leads.find_one({"_id": lead["_id"]})["assigned_agent_source"] == "manual"  # pin kept


def test_out_of_range_agent_index_falls_back_to_general(db, router):
    _agent(db, "Only")
    lead = _lead(db)
    _msg(db, lead, "inbound", "something")
    router["answer"]["value"] = {"route": "agent", "agent": 7, "continues_previous": False, "search_query": "x"}
    assert route_message(db, UID, lead).agent is None


def test_default_agent_takes_general_messages_when_tenant_set_one(db, router):
    _agent(db, "Specialist", description="x")
    catch_all = _agent(db, "Front desk", is_default=True)
    lead = _lead(db)
    _msg(db, lead, "inbound", "hello")
    d = route_message(db, UID, lead)
    assert d.agent["_id"] == catch_all["_id"] and d.mode == "default"


def test_campaign_message_is_attributed_to_its_agent(db, router):
    ws = _agent(db, "Workshop Agent")
    camp = db.campaigns.insert_one({"user_id": UID, "agent_id": str(ws["_id"])}).inserted_id
    lead = _lead(db)
    _msg(db, lead, "outbound", "Join our AI weekend workshop this Saturday!", minutes_ago=60,
         campaign_id=str(camp), message_purpose="campaign")
    _msg(db, lead, "inbound", "yes interested, what time?")
    turns = conversation_turns(db, UID, str(lead["_id"]))
    assert turns[0] == {"who": "Campaign message from Workshop Agent", "text": "Join our AI weekend workshop this Saturday!"}


def test_old_conversation_is_not_context(db, router):
    _agent(db, "A")
    lead = _lead(db)
    _msg(db, lead, "inbound", "can you talk about internship", minutes_ago=60 * 24 * 18)
    _msg(db, lead, "inbound", "Hey hi how are you")
    route_message(db, UID, lead)
    assert router["calls"][0]["turns"] == [{"who": "Customer", "text": "Hey hi how are you"}]


def test_message_text_argument_is_used_when_not_yet_stored(db, router):
    _agent(db, "A")
    lead = _lead(db)
    route_message(db, UID, lead, message_text="first ever message")
    assert router["calls"][0]["turns"][-1] == {"who": "Customer", "text": "first ever message"}


# --- When the routing model is unavailable ----------------------------------------------

@pytest.fixture
def no_model(monkeypatch):
    monkeypatch.setattr(agent_router, "_llm_route", lambda *a, **k: None)


def test_fallback_keywords(db, no_model):
    _agent(db, "Train", keywords=["train", "pnr"])
    _agent(db, "Food", keywords=["pizza"])
    lead = _lead(db)
    _msg(db, lead, "inbound", "whats my PNR status")
    d = route_message(db, UID, lead)
    assert d.agent["name"] == "Train" and d.via == "fallback"


def test_fallback_small_talk_is_general(db, no_model):
    a = _agent(db, "Train", keywords=["train"])
    lead = _lead(db)
    _msg(db, lead, "outbound", "Your ticket is booked.", minutes_ago=2, agent_name=a["name"], sender_type="ai")
    _msg(db, lead, "inbound", "thanks")
    assert route_message(db, UID, lead).agent is None


def test_fallback_follow_up_continues_with_previous_agent(db, no_model):
    _agent(db, "Train", keywords=["train"])
    _agent(db, "Food", keywords=["pizza"])
    lead = _lead(db)
    _msg(db, lead, "inbound", "book a train to leeds", minutes_ago=3)
    _msg(db, lead, "outbound", "Which date?", minutes_ago=2, agent_name="Train", sender_type="ai")
    _msg(db, lead, "inbound", "next friday morning")
    d = route_message(db, UID, lead)
    assert d.agent["name"] == "Train" and d.continues is True


def test_fallback_without_context_is_general(db, no_model):
    _agent(db, "Train", keywords=["train"])
    lead = _lead(db)
    _msg(db, lead, "inbound", "what's the capital of france?")
    assert route_message(db, UID, lead).agent is None


def test_select_agent_for_inbound_returns_the_routed_agent(db, router):
    a = _agent(db, "Only", description="x")
    lead = _lead(db)
    _msg(db, lead, "inbound", "question about x")
    router["answer"]["value"] = {"route": "agent", "agent": 0, "continues_previous": False, "search_query": "x"}
    assert select_agent_for_inbound(db, UID, lead)["_id"] == a["_id"]


# --- Keyword helpers (used by the fallback) ------------------------------------------------

def test_keyword_word_boundary_no_false_positive():
    assert agent_router._keyword_score({"routing_keywords": ["cat"]}, "what category") == 0
    assert agent_router._keyword_score({"routing_keywords": ["cat"]}, "my cat is ill") == 1
    assert agent_router._keyword_score({"routing_keywords": ["book a table"]}, "can I book a table?") == 1


# --- General assistant prompt ------------------------------------------------------------

def test_general_prompt_is_friendly_neutral_and_lists_topics_without_pushing():
    from app.services.ai_prompt import build_system_prompt

    ai = {"ai_business_description": "BOSS Braintree luxury retail", "ai_custom_instructions": "Always upsell watches",
          "ai_disallowed_topics": "politics"}
    p = build_system_prompt(agent=None, ai_settings=ai, neutral=True,
                            neutral_topics=["AI testing courses", "Restaurant menu and bookings"])
    assert "BOSS Braintree" not in p and "upsell watches" not in p   # no business identity leak
    assert "friendly, helpful WhatsApp assistant" in p
    assert "politics" in p                                           # safety retained
    assert "- AI testing courses" in p and "never push it" in p


# --- First-contact welcome message -------------------------------------------------------

def test_welcome_agent_single_agent_tenant_needs_no_model(db, router):
    a = _agent(db, "Solo")
    assert agent_router.welcome_agent(db, UID, _lead(db))["_id"] == a["_id"] and router["calls"] == []


def test_welcome_agent_prefers_default_then_routes(db, router):
    _agent(db, "A")
    d = _agent(db, "Front desk", is_default=True)
    assert agent_router.welcome_agent(db, UID, _lead(db))["_id"] == d["_id"] and router["calls"] == []



@pytest.mark.parametrize("answer,expected", [
    ({"agent_name": "Agent_1"}, "Agent_1"),
    ({"agent_name": "agent_1"}, "Agent_1"),                      # case-insensitive
    ({"agent_name": "AI Testing Hub"}, "AI Testing Hub Agent"),   # unique partial
    ({"agent_name": "Nope"}, None),
    ({"agent": 0}, "AI Testing Hub Agent"),                       # legacy index still works
])
def test_router_answer_is_matched_by_agent_name(db, answer, expected):
    agents = [_agent(db, "AI Testing Hub Agent"), _agent(db, "Agent_1"), _agent(db, "Agent_3")]
    got = agent_router._match_agent(agents, answer)
    assert (got or {}).get("name") == expected


def test_business_question_left_general_goes_to_the_single_keyword_agent(db, router):
    _agent(db, "Train", keywords=["train"])
    rest = _agent(db, "Restaurant", keywords=["restaurant", "menu"])
    lead = _lead(db)
    _msg(db, lead, "inbound", "also is the restaurant open on Sunday?")
    router["answer"]["value"] = {"route": "general", "agent_name": None, "about_business": True,
                                 "continues_previous": False, "search_query": ""}
    d = route_message(db, UID, lead)
    assert d.agent["_id"] == rest["_id"] and d.via == "llm+keywords"


def test_pronoun_follow_up_uses_previous_message_keywords(db, router):
    course = _agent(db, "AI course", keywords=["machine learning"])
    _agent(db, "Restaurant", keywords=["menu"])
    lead = _lead(db)
    _msg(db, lead, "inbound", "what's machine learning?", minutes_ago=2)
    _msg(db, lead, "outbound", "It's a way computers learn from data.", minutes_ago=1, sender_type="ai")
    _msg(db, lead, "inbound", "do you have a course on it?")
    router["answer"]["value"] = {"route": "general", "agent_name": None, "about_business": True,
                                 "continues_previous": True, "search_query": ""}
    assert route_message(db, UID, lead).agent["_id"] == course["_id"]


def test_general_knowledge_question_stays_general_despite_keywords(db, router):
    _agent(db, "AI course", keywords=["machine learning"])
    lead = _lead(db)
    _msg(db, lead, "inbound", "what's machine learning in simple terms?")
    router["answer"]["value"] = {"route": "general", "agent_name": None, "about_business": False,
                                 "continues_previous": False, "search_query": ""}
    assert route_message(db, UID, lead).agent is None


def test_comparing_two_businesses_stays_general(db, router):
    _agent(db, "AI Testing Hub Agent", keywords=["ai testing hub"])
    _agent(db, "AI Agent Hub Agent", keywords=["ai agent hub"])
    lead = _lead(db)
    _msg(db, lead, "inbound", "what's the difference between AI Testing Hub and AI Agent Hub?")
    router["answer"]["value"] = {"route": "general", "agent_name": None, "about_business": True,
                                 "continues_previous": False, "search_query": ""}
    assert route_message(db, UID, lead).agent is None


def test_descriptive_name_from_model_maps_to_the_agent_by_description(db):
    agents = [_agent(db, "Agent_2", description="AI course — enrolment, details, invitations"),
              _agent(db, "Agent_1", description="Restaurant — new opening, menu, inaugural offers, reservations"),
              _agent(db, "AI Testing Hub Agent", description="AI testing consulting and course")]
    got = agent_router._match_agent(agents, {"agent_name": "AI Course Agent"})
    assert got["name"] == "Agent_2"
    assert agent_router._match_agent(agents, {"agent_name": "Restaurant Agent"})["name"] == "Agent_1"
    assert agent_router._match_agent(agents, {"agent_name": "Plumbing Agent"}) is None
