"""An agent linked to a knowledge base that has no content yet keeps using its own pasted
knowledge — linking an empty KB must not silence it."""
from unittest.mock import patch

import mongomock
import pytest
from bson import ObjectId

from app.services.kb import retrieve
from app.services.kb.retrieve import KBContext, retrieve_for_reply

UID = "tenant1"
HISTORY = [{"role": "user", "content": "I want to know about exam revision"}]


@pytest.fixture
def db():
    return mongomock.MongoClient().db


def _kb(db):
    return str(db.knowledge_bases.insert_one({"user_id": UID, "name": "AI Exam Revision Coach Knowledge"}).inserted_id)


def _agent(kb_id, legacy="Smart Study Academy offers online revision support for Maths, English and Science."):
    return {"_id": ObjectId(), "name": "AI Exam Revision Coach", "knowledge_base_ids": [kb_id], "knowledge_base": legacy}


def test_empty_linked_kb_falls_back_to_the_agents_pasted_knowledge(db):
    kb_id = _kb(db)
    with patch.object(retrieve, "search") as search:
        ctx = retrieve_for_reply(db, user_id=UID, agent=_agent(kb_id), history=HISTORY, query="exam revision")
    assert ctx is None  # callers then use the agent's pasted knowledge, no "nothing matched" guard
    search.assert_not_called()


def test_linked_kb_with_content_is_searched_as_before(db):
    kb_id = _kb(db)
    db.kb_chunks.insert_one({"user_id": UID, "kb_id": kb_id, "text": "Sessions start from GBP 20."})
    with patch.object(retrieve, "search", return_value=[]) as search:
        ctx = retrieve_for_reply(db, user_id=UID, agent=_agent(kb_id), history=HISTORY, query="exam revision")
    assert isinstance(ctx, KBContext) and ctx.searched and ctx.hits == []
    search.assert_called_once()


def test_empty_kb_without_pasted_knowledge_keeps_the_no_match_guard(db):
    kb_id = _kb(db)
    with patch.object(retrieve, "search", return_value=[]):
        ctx = retrieve_for_reply(db, user_id=UID, agent=_agent(kb_id, legacy=""), history=HISTORY, query="q")
    assert isinstance(ctx, KBContext) and ctx.searched  # unchanged: "say you'll check with the team"


def test_another_tenants_chunks_dont_count(db):
    kb_id = _kb(db)
    db.kb_chunks.insert_one({"user_id": "someone_else", "kb_id": kb_id, "text": "x"})
    assert retrieve_for_reply(db, user_id=UID, agent=_agent(kb_id), history=HISTORY, query="q") is None


def test_reply_prompt_uses_the_pasted_knowledge_when_the_kb_is_empty(db):
    from app.services.ai_prompt import build_system_prompt

    kb_id = _kb(db)
    agent = _agent(kb_id)
    ctx = retrieve_for_reply(db, user_id=UID, agent=agent, history=HISTORY, query="q")
    prompt = build_system_prompt(agent=agent, kb_context=ctx)
    assert "KNOWLEDGE BASE:\nSmart Study Academy offers online revision support" in prompt
