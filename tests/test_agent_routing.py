"""Inbound agent routing (Phase 1)."""
from unittest.mock import MagicMock, patch

from bson import ObjectId

from app.services import agent_router


def _agent(name, *, keywords=None, is_default=False, description="", updated=0):
    return {
        "_id": ObjectId(),
        "name": name,
        "status": "active",
        "routing_keywords": keywords or [],
        "is_default": is_default,
        "description": description,
        "updated_at": updated,
    }


def _db(agents, latest_inbound=""):
    db = MagicMock()
    cur = MagicMock()
    cur.sort = MagicMock(return_value=list(agents))
    db.agents.find = MagicMock(return_value=cur)
    db.messages.find_one = MagicMock(return_value={"message": latest_inbound})
    db.leads.update_one = MagicMock()
    return db


def test_no_agents_returns_none():
    db = _db([])
    assert agent_router.select_agent_for_inbound(db, "u", {"_id": ObjectId()}) is None


def test_single_agent_always_wins_and_persists():
    a = _agent("Solo")
    db = _db([a])
    lead = {"_id": ObjectId()}
    out = agent_router.select_agent_for_inbound(db, "u", lead)
    assert out["name"] == "Solo"
    db.leads.update_one.assert_called_once()  # persisted assigned_agent_id


def test_sticky_assignment_beats_routing():
    train = _agent("Train")
    food = _agent("Food", keywords=["pizza"])
    db = _db([train, food])
    lead = {"_id": ObjectId(), "assigned_agent_id": str(train["_id"])}
    # message screams "food" but the conversation is already stuck to Train
    out = agent_router.select_agent_for_inbound(db, "u", lead, message_text="I want a pizza")
    assert out["name"] == "Train"


def test_keyword_routing_picks_right_agent():
    train = _agent("Train", keywords=["train", "pnr", "irctc"])
    food = _agent("Food", keywords=["pizza", "order", "food"])
    db = _db([train, food])
    lead = {"_id": ObjectId()}
    out = agent_router.select_agent_for_inbound(db, "u", lead, message_text="whats my PNR status")
    assert out["name"] == "Train"
    out2 = agent_router.select_agent_for_inbound(db, "u", lead, message_text="I want to order pizza")
    assert out2["name"] == "Food"


def test_keyword_word_boundary_no_false_positive():
    a = _agent("Cat", keywords=["cat"])
    b = _agent("Other", keywords=["dog"], is_default=True)
    db = _db([a, b])
    # "category" must NOT match keyword "cat" (word boundary) -> falls through to default
    with patch.object(agent_router, "_llm_pick", return_value=None):
        out = agent_router.select_agent_for_inbound(db, "u", {"_id": ObjectId()}, message_text="what category")
    assert out["name"] == "Other"


def test_llm_router_used_when_keywords_dont_decide():
    a = _agent("Billing", description="invoices and payments")
    b = _agent("Tech", description="technical support")
    db = _db([a, b])
    with patch.object(agent_router, "_llm_pick", return_value=b) as llm:
        out = agent_router.select_agent_for_inbound(db, "u", {"_id": ObjectId()}, message_text="my app crashes")
    llm.assert_called_once()
    assert out["name"] == "Tech"


def test_default_agent_when_nothing_matches():
    a = _agent("A")
    b = _agent("B", is_default=True)
    db = _db([a, b])
    with patch.object(agent_router, "_llm_pick", return_value=None):
        out = agent_router.select_agent_for_inbound(db, "u", {"_id": ObjectId()}, message_text="hello there")
    assert out["name"] == "B"


def test_no_match_no_default_returns_none_for_generic_llm():
    # 3 agents (job/camp/physics), message matches none, no default → generic LLM (None).
    job = _agent("Job Support", keywords=["job", "cv", "resume"])
    camp = _agent("Summer Camp", keywords=["camp", "summer"])
    physics = _agent("Physics", keywords=["physics", "motion", "force"])
    db = _db([job, camp, physics])
    with patch.object(agent_router, "_llm_pick", return_value=None):
        out = agent_router.select_agent_for_inbound(
            db, "u", {"_id": ObjectId()}, message_text="what's the weather today?"
        )
    assert out is None  # caller replies with a generic LLM, not a forced agent
    db.leads.update_one.assert_not_called()  # no owner persisted


import pytest


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Can you review my CV for a job?", "Job Support"),
        ("interview tips for hiring", "Job Support"),
        ("when does summer camp start, enrol my kids", "Summer Camp"),
        ("explain newton's law of motion", "Physics"),
        ("force to accelerate a 2kg object", "Physics"),
        ("what's the weather today?", None),   # generic
        ("tell me a joke", None),              # generic
    ],
)
def test_all_combos_three_agents(message, expected):
    job = _agent("Job Support", keywords=["job", "cv", "resume", "interview", "hiring"])
    camp = _agent("Summer Camp", keywords=["camp", "summer", "enrol", "kids"])
    physics = _agent("Physics", keywords=["physics", "motion", "force", "newton", "energy"])
    db = _db([physics, camp, job])
    with patch.object(agent_router, "_llm_pick", return_value=None):
        out = agent_router.select_agent_for_inbound(db, "u", {"_id": ObjectId()}, message_text=message)
    assert (out["name"] if out else None) == expected


def test_neutral_generic_prompt_drops_business_keeps_safety():
    from app.services.ai_prompt import build_system_prompt

    ai = {
        "ai_business_description": "BOSS Braintree luxury retail",
        "ai_custom_instructions": "Always upsell watches",
        "ai_disallowed_topics": "politics",
    }
    p = build_system_prompt(agent=None, ai_settings=ai, neutral=True)
    assert "BOSS Braintree" not in p          # no business identity leak
    assert "upsell watches" not in p          # no tenant sales instructions
    assert "neutral WhatsApp assistant" in p  # explicitly neutral
    assert "politics" in p                     # safety/disallowed retained


def test_no_match_but_default_set_uses_default_not_generic():
    job = _agent("Job Support", keywords=["job"])
    catch_all = _agent("General", is_default=True)
    db = _db([job, catch_all])
    with patch.object(agent_router, "_llm_pick", return_value=None):
        out = agent_router.select_agent_for_inbound(
            db, "u", {"_id": ObjectId()}, message_text="random question"
        )
    assert out["name"] == "General"
