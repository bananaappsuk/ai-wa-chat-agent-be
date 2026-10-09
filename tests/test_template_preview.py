"""Template preview: users see a template's message text before they pick it for a blast or campaign."""
from unittest.mock import patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.routes import templates as templates_route

USER = {"_id": ObjectId()}
BODY = "Invitation to AI Engineer in Test Meetup – London\n\nHi\nDate: Tuesday, 13 October"


@pytest.fixture(autouse=True)
def fresh_template_cache():
    from app.services import ai_campaign

    ai_campaign._TEMPLATE_BODY_CACHE.clear()
    yield
    ai_campaign._TEMPLATE_BODY_CACHE.clear()


async def _preview(db, tid, user=USER):
    with patch.object(templates_route, "get_db", return_value=db):
        return await templates_route.preview_template(str(tid), user=user)


@pytest.mark.asyncio
async def test_twilio_template_shows_its_approved_text():
    db = AsyncMongoMockClient().db
    tid = (await db.templates.insert_one({"user_id": str(USER["_id"]), "name": "aiagent_meetup_london",
                                          "provider": "twilio_content", "content_sid": "HX32305b4f57a9c184aba2c60e8f70f9fa"})).inserted_id
    with patch("app.services.twilio_service.get_content_template_info", return_value={"body": BODY}) as info:
        out = await _preview(db, tid)
        await _preview(db, tid)
    assert out["body"] == BODY and out["name"] == "aiagent_meetup_london"
    assert info.call_count == 1  # cached: opening the preview again doesn't call Twilio again


@pytest.mark.asyncio
async def test_meta_template_shows_header_body_and_footer():
    db = AsyncMongoMockClient().db
    tid = (await db.templates.insert_one({"user_id": str(USER["_id"]), "name": "order_update", "provider": "meta",
                                          "components": [{"type": "FOOTER", "text": "Reply STOP to opt out"},
                                                         {"type": "BODY", "text": "Your order {{1}} has shipped."},
                                                         {"type": "HEADER", "format": "TEXT", "text": "Order update"},
                                                         {"type": "BUTTONS", "buttons": []}]})).inserted_id
    out = await _preview(db, tid)
    assert out["body"] == "Order update\n\nYour order {{1}} has shipped.\n\nReply STOP to opt out"


@pytest.mark.asyncio
async def test_another_accounts_template_is_not_shown():
    db = AsyncMongoMockClient().db
    tid = (await db.templates.insert_one({"user_id": "someone-else", "name": "x", "content_sid": "HXabc"})).inserted_id
    with pytest.raises(HTTPException) as exc:
        await _preview(db, tid)
    assert exc.value.status_code == 404
