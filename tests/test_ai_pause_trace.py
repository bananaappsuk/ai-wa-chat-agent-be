"""Pause AI is traceable (who/when on the contact + activity entry) and markdown links are
made WhatsApp-safe."""
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.routes import leads as leads_route
from app.services.ai_quality import post_process
from app.services.lead_bulk import run_bulk_action

UID = str(ObjectId())


@pytest.fixture
def db():
    return AsyncMongoMockClient().db


@pytest.fixture
def routes(db):
    with patch("app.services.lead_service.get_db", return_value=db), \
         patch.object(leads_route, "get_db", return_value=db), \
         patch.object(leads_route.ws_manager, "push", new=AsyncMock()):
        yield db


async def _lead(db, **kw):
    doc = {"user_id": UID, "phone": "+447700900001", "name": "Timiya", "ai_paused": False, **kw}
    doc["_id"] = (await db.leads.insert_one(doc)).inserted_id
    return str(doc["_id"])


@pytest.mark.asyncio
async def test_pause_records_who_and_when_and_an_activity_entry(routes):
    lid = await _lead(routes)
    out = await leads_route.pause_ai(lid, user={"_id": ObjectId(UID)})
    assert out["ai_paused"] is True and out["ai_paused_by"] == UID and out["ai_paused_at"]
    ev = await routes.activity_events.find_one({"resource_id": lid})
    assert ev["event_type"] == "lead.ai_pause" and ev["actor_id"] == UID
    assert ev["summary"] == "AI paused for this contact"


@pytest.mark.asyncio
async def test_resume_clears_the_pause_and_is_recorded(routes):
    lid = await _lead(routes)
    await leads_route.pause_ai(lid, user={"_id": ObjectId(UID)})
    out = await leads_route.resume_ai(lid, user={"_id": ObjectId(UID)})
    assert out["ai_paused"] is False and out.get("ai_paused_at") is None and out.get("ai_paused_by") is None
    types = [e["event_type"] async for e in routes.activity_events.find({"resource_id": lid}).sort("_id", 1)]
    assert types == ["lead.ai_pause", "lead.ai_resume"]


@pytest.mark.asyncio
async def test_handback_clears_the_pause_time(routes):
    lid = await _lead(routes, ai_paused=True, ai_paused_at="x", ai_paused_by=UID, takeover_by=UID)
    out = await leads_route.hand_back(lid, user={"_id": ObjectId(UID)})
    assert out["ai_paused"] is False and out.get("ai_paused_at") is None and out.get("takeover_by") is None


@pytest.mark.asyncio
async def test_bulk_pause_and_resume_are_recorded_per_contact(db):
    a, b = await _lead(db), await _lead(db, phone="+447700900002")
    with patch("app.services.lead_bulk.ws_manager.push", new=AsyncMock()), \
         patch("app.services.lead_scoring.recalculate_lead_score", new=AsyncMock()):
        res = await run_bulk_action(db, user_id=UID, lead_ids=[a, b], action="pause_ai")
        assert res["affected"] == 2
        for lid in (a, b):
            lead = await db.leads.find_one({"_id": ObjectId(lid)})
            assert lead["ai_paused"] is True and lead["ai_paused_by"] == UID and lead["ai_paused_at"]
        await run_bulk_action(db, user_id=UID, lead_ids=[a], action="resume_ai")
    lead_a = await db.leads.find_one({"_id": ObjectId(a)})
    assert lead_a["ai_paused"] is False and lead_a["ai_paused_at"] is None
    summaries = [e["summary"] async for e in db.activity_events.find({"resource_id": a}).sort("_id", 1)]
    assert summaries == ["AI paused for this contact (bulk)", "AI resumed for this contact (bulk)"]


@pytest.mark.parametrize("raw, expected", [
    ("Register here: [Register for the workshop](https://ittalenthub.co.uk).",
     "Register here: Register for the workshop: https://ittalenthub.co.uk."),
    ("Book: [https://ittalenthub.co.uk](https://ittalenthub.co.uk)", "Book: https://ittalenthub.co.uk"),
    ("See [ittalenthub.co.uk](https://ittalenthub.co.uk/book) now", "See https://ittalenthub.co.uk/book now"),
    ("Two: [A](https://a.com) and [B](http://b.com/x?y=1)", "Two: A: https://a.com and B: http://b.com/x?y=1"),
    ("No link [just brackets] here (and parens)", "No link [just brackets] here (and parens)"),
    ("Plain https://ittalenthub.co.uk stays", "Plain https://ittalenthub.co.uk stays"),
])
def test_markdown_links_become_plain_whatsapp_text(raw, expected):
    assert post_process(raw) == expected


def test_prompt_forbids_markdown_links():
    from app.services.ai_prompt import CORE_RULES

    assert "[text](link)" in CORE_RULES and "write the URL itself" in CORE_RULES
