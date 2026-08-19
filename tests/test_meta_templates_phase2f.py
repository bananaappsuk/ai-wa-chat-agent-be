"""Phase 2F Meta WhatsApp templates — sync + Live Chat send (no live Graph)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx
import pytest
from bson import ObjectId
from fastapi import HTTPException

from app.models.message import MessageSend
from app.routes import messages as messages_route
from app.routes import templates as templates_route
from app.services import meta_templates as mt
from app.services.meta_whatsapp_service import send_template
from app.services.status_callback import apply_meta_status_update
from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility
from app.workers import tasks
from tests.test_meta_inbound_phase2a import MemColl, MemDB


def _mem() -> MemDB:
    m = MemDB()
    m.templates = MemColl()
    return m
from tests.test_meta_livechat_phase2c import _closed_lead, _open_lead, _send


TOKEN = "secret-token-NEVER-STORE"
WABA = "WABA123"
PNID = "PN_POC"


def _approved_meta_tmpl(user_id: str, tmpl_id: ObjectId | None = None) -> dict:
    schema = [
        {"key": "1", "kind": "body", "index": 0, "param_type": "text", "required": True},
        {"key": "header", "kind": "header", "index": 0, "param_type": "text", "required": True},
        {
            "key": "button:0",
            "kind": "button",
            "index": 0,
            "sub_type": "url",
            "param_type": "text",
            "required": True,
        },
    ]
    return {
        "_id": tmpl_id or ObjectId(),
        "user_id": user_id,
        "provider": "meta",
        "name": "order_update",
        "meta_template_name": "order_update",
        "meta_language_code": "en_US",
        "status": "approved",
        "whatsapp_approval_status": "approved",
        "whatsapp_approval_status_raw": "APPROVED",
        "whatsapp_category": "UTILITY",
        "components": [
            {"type": "HEADER", "format": "TEXT", "text": "Hi {{1}}"},
            {"type": "BODY", "text": "Order {{1}} ready"},
            {
                "type": "BUTTONS",
                "buttons": [{"type": "URL", "text": "Track", "url": "https://ex.com/{{1}}"}],
            },
        ],
        "variable_schema": schema,
        "variables": ["1", "header", "button:0"],
        "send_supported": True,
        "content_sid": None,
    }


def test_graph_status_normalized():
    assert mt.map_graph_status("APPROVED") == "approved"
    assert mt.map_graph_status("PENDING") in ("pending", "under_review")
    assert mt.map_graph_status("IN_APPEAL") == "under_review"
    assert mt.map_graph_status("REJECTED") == "rejected"
    assert mt.map_graph_status("PAUSED") == "paused"
    assert mt.map_graph_status("DISABLED") == "paused"


def test_body_header_button_mapping_order():
    tmpl = _approved_meta_tmpl("u1")
    comps = mt.build_graph_components(
        template=tmpl,
        content_variables={"1": "A1", "header": "Hello", "button:0": "abc"},
    )
    types = [c["type"] for c in comps]
    assert types == ["header", "body", "button"]
    assert comps[1]["parameters"][0]["text"] == "A1"
    assert comps[0]["parameters"][0]["text"] == "Hello"
    assert comps[2]["sub_type"] == "url"
    assert comps[2]["index"] == "0"


def test_missing_variable_and_extra_rejected():
    tmpl = _approved_meta_tmpl("u1")
    with pytest.raises(mt.MetaTemplateError, match="Missing"):
        mt.build_graph_components(template=tmpl, content_variables={"1": "x"})
    with pytest.raises(mt.MetaTemplateError, match="Unexpected"):
        mt.build_graph_components(
            template=tmpl,
            content_variables={"1": "a", "header": "h", "button:0": "b", "extra": "z"},
        )


def test_media_header_not_send_supported():
    ok, reason = mt.analyze_send_support(
        [{"type": "HEADER", "format": "IMAGE"}],
        "UTILITY",
    )
    assert ok is False
    assert "Media header" in (reason or "")
    ok2, _ = mt.analyze_send_support([], "AUTHENTICATION")
    assert ok2 is False


def test_poc_tenant_guards():
    user = {"_id": ObjectId(), "meta_phone_number_id": "PN_OTHER"}
    with (
        patch.object(mt.settings, "META_ACCESS_TOKEN", TOKEN),
        patch.object(mt.settings, "META_WABA_ID", WABA),
        patch.object(mt.settings, "META_PHONE_NUMBER_ID", PNID),
        pytest.raises(HTTPException) as exc,
    ):
        mt.assert_poc_meta_template_tenant(user)
    assert exc.value.status_code == 403

    with (
        patch.object(mt.settings, "META_ACCESS_TOKEN", ""),
        patch.object(mt.settings, "META_WABA_ID", WABA),
        patch.object(mt.settings, "META_PHONE_NUMBER_ID", PNID),
        pytest.raises(HTTPException) as exc2,
    ):
        mt.assert_poc_meta_template_tenant({"_id": ObjectId(), "meta_phone_number_id": PNID})
    assert exc2.value.status_code == 400

    with (
        patch.object(mt.settings, "META_ACCESS_TOKEN", TOKEN),
        patch.object(mt.settings, "META_WABA_ID", ""),
        patch.object(mt.settings, "META_PHONE_NUMBER_ID", PNID),
        pytest.raises(HTTPException) as exc3,
    ):
        mt.assert_poc_meta_template_tenant({"_id": ObjectId(), "meta_phone_number_id": PNID})
    assert exc3.value.status_code == 400


@pytest.mark.asyncio
async def test_sync_uses_bearer_and_pagination():
    seen: list[httpx.Request] = []
    page2 = "https://graph.facebook.com/v21.0/WABA123/message_templates?after=CUR"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers.get("Authorization") == f"Bearer {TOKEN}"
        if "after=CUR" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "2",
                            "name": "t2",
                            "language": "en_US",
                            "status": "APPROVED",
                            "category": "UTILITY",
                            "components": [{"type": "BODY", "text": "Hi {{1}}"}],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "1",
                        "name": "t1",
                        "language": "en_US",
                        "status": "PENDING",
                        "category": "MARKETING",
                        "components": [{"type": "BODY", "text": "Hello"}],
                    }
                ],
                "paging": {"next": page2},
            },
        )

    class _C(httpx.AsyncClient):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            k["follow_redirects"] = False
            super().__init__(*a, **k)

    mem = _mem()
    user = {"_id": ObjectId(), "meta_phone_number_id": PNID}
    with (
        patch.object(mt.settings, "META_ACCESS_TOKEN", TOKEN),
        patch.object(mt.settings, "META_WABA_ID", WABA),
        patch.object(mt.settings, "META_PHONE_NUMBER_ID", PNID),
        patch.object(mt.settings, "META_GRAPH_VERSION", "v21.0"),
        patch("app.services.meta_templates.httpx.AsyncClient", _C),
    ):
        result = await mt.sync_meta_templates_for_user(mem, user=user)
    assert result["synced"] == 2
    assert len(seen) == 2
    rows = mem.templates.docs
    assert all(r["provider"] == "meta" for r in rows)
    assert all(r.get("content_sid") in (None, "") for r in rows)
    t1 = next(r for r in rows if r["meta_template_name"] == "t1")
    assert t1["meta_language_code"] == "en_US"
    assert t1["whatsapp_approval_status"] in ("pending", "under_review")
    t2 = next(r for r in rows if r["meta_template_name"] == "t2")
    assert t2["whatsapp_approval_status"] == "approved"
    blob = str(rows)
    assert TOKEN not in blob
    hosts = {urlparse(str(r.url)).hostname for r in seen}
    assert hosts == {"graph.facebook.com"}


@pytest.mark.asyncio
async def test_wrong_pnid_cannot_sync():
    mem = _mem()
    user = {"_id": ObjectId(), "meta_phone_number_id": "NOPE"}
    with (
        patch.object(mt.settings, "META_ACCESS_TOKEN", TOKEN),
        patch.object(mt.settings, "META_WABA_ID", WABA),
        patch.object(mt.settings, "META_PHONE_NUMBER_ID", PNID),
        pytest.raises(HTTPException) as exc,
    ):
        await mt.sync_meta_templates_for_user(mem, user=user)
    assert exc.value.status_code == 403
    assert mem.templates.docs == []


def test_send_template_graph_payload_and_no_token_in_result():
    captured = {}

    class FakeResp:
        is_error = False
        status_code = 200

        def json(self):
            return {"messages": [{"id": "wamid.TPL"}]}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResp()

    with (
        patch("app.services.meta_whatsapp_service.settings") as st,
        patch("app.services.meta_whatsapp_service.httpx.Client", FakeClient),
    ):
        st.META_ACCESS_TOKEN = TOKEN
        st.META_PHONE_NUMBER_ID = PNID
        st.META_GRAPH_VERSION = "v21.0"
        st.META_HTTP_TIMEOUT_SECONDS = 30
        result = send_template(
            to="+447700900123",
            name="order_update",
            language_code="en_US",
            components=[{"type": "body", "parameters": [{"type": "text", "text": "x"}]}],
        )
    assert captured["json"]["type"] == "template"
    assert captured["json"]["template"]["name"] == "order_update"
    assert captured["json"]["template"]["language"]["code"] == "en_US"
    assert "/messages" in captured["url"]
    assert captured["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert result.provider_message_id == "wamid.TPL"
    assert TOKEN not in str(result.raw)


def test_eligibility_meta_closed_window():
    now = datetime.now(timezone.utc)
    lead = {
        "phone": "+447700900123",
        "blacklisted": False,
        "whatsapp_consent_status": "opted_in",
        "last_inbound_at": now - timedelta(hours=30),
        "whatsapp_window_expires_at": now - timedelta(hours=1),
    }
    with patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True):
        blocked = get_whatsapp_send_eligibility(
            lead=lead, purpose="conversational", has_template=False, provider="meta"
        )
        allowed = get_whatsapp_send_eligibility(
            lead=lead, purpose="conversational", has_template=True, provider="meta"
        )
    assert blocked.allowed is False
    assert blocked.template_required is True
    assert "approved Meta WhatsApp template" in blocked.safe_message
    assert allowed.allowed is True


async def _send_meta_tpl(lead, user, tmpl, variables, inbound_provider="meta"):
    insert_doc = {
        "_id": ObjectId(),
        "user_id": str(user["_id"]),
        "lead_id": str(lead["_id"]),
        "direction": "outbound",
        "message": "Template: order_update",
        "status": "queued",
        "created_at": datetime.now(timezone.utc),
    }
    enqueue = MagicMock()
    push = AsyncMock()
    insert = AsyncMock(return_value=insert_doc)

    async def find_one(query=None, **kwargs):
        if (query or {}).get("direction") == "inbound":
            return {"direction": "inbound", "provider": inbound_provider}
        return {**insert_doc, "provider": inbound_provider, "sender_type": "human"}

    db = MagicMock()
    db.messages = MagicMock(update_one=AsyncMock(), find_one=AsyncMock(side_effect=find_one))
    db.templates = MagicMock(find_one=AsyncMock(return_value=tmpl))
    db.users = MagicMock(find_one=AsyncMock(return_value=user))
    with (
        patch.object(messages_route.lead_service, "get_lead", new=AsyncMock(return_value=lead)),
        patch.object(messages_route.message_service, "insert_message", new=insert),
        patch.object(messages_route.ws_manager, "push", new=push),
        patch.object(messages_route, "enqueue", enqueue),
        patch("app.security.rate_limit.rate_limit_send"),
        patch("app.services.whatsapp_eligibility._sender_ok", return_value=True),
        patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True),
        patch.object(messages_route, "get_db", return_value=db),
        patch.object(messages_route, "get_sendable_meta_template", new=AsyncMock(return_value=tmpl)),
        patch.object(mt, "assert_poc_meta_template_tenant", return_value=None),
        patch.object(templates_route, "get_db", return_value=db),
        patch.object(mt.settings, "META_ACCESS_TOKEN", TOKEN),
        patch.object(mt.settings, "META_WABA_ID", WABA),
        patch.object(mt.settings, "META_PHONE_NUMBER_ID", PNID),
    ):
        result = await messages_route.send_message(
            MessageSend(
                lead_id=str(lead["_id"]),
                template_id=str(tmpl["_id"]),
                content_variables=variables,
                message_purpose="transactional",
            ),
            user=user,
        )
    return result, enqueue, insert, db


@pytest.mark.asyncio
async def test_closed_meta_window_template_allowed():
    user = {"_id": ObjectId(), "meta_phone_number_id": PNID}
    lead = _closed_lead(str(user["_id"]), ObjectId())
    tmpl = _approved_meta_tmpl(str(user["_id"]))
    result, enq, insert, db = await _send_meta_tpl(
        lead, user, tmpl, {"1": "42", "header": "Hi", "button:0": "zz"}
    )
    assert result["status"] == "queued"
    assert insert.await_args.kwargs["provider"] == "meta"
    assert insert.await_args.kwargs["message_type"] == "template"
    assert insert.await_args.kwargs["content_sid"] is None
    extra = db.messages.update_one.await_args.args[1]["$set"]
    assert extra["meta_template_name"] == "order_update"
    assert extra["meta_language_code"] == "en_US"
    assert extra.get("twilio_sid") is None
    assert extra.get("content_sid") is None
    enq.assert_called_once()
    assert enq.call_args.kwargs.get("content_sid") is None


@pytest.mark.asyncio
async def test_pending_rejected_paused_blocked():
    user = {"_id": ObjectId(), "meta_phone_number_id": PNID}
    lead = _open_lead(str(user["_id"]), ObjectId())
    for st in ("pending", "rejected", "paused"):
        tmpl = _approved_meta_tmpl(str(user["_id"]))
        tmpl["whatsapp_approval_status"] = st
        tmpl["send_supported"] = True
        with pytest.raises(HTTPException) as exc:
            await _send_meta_tpl(lead, user, tmpl, {"1": "1", "header": "h", "button:0": "b"})
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_twilio_thread_rejects_meta_template_id():
    user = {"_id": ObjectId()}
    lead = _open_lead(str(user["_id"]), ObjectId())
    tmpl = _approved_meta_tmpl(str(user["_id"]))
    insert = AsyncMock()
    enqueue = MagicMock()
    db = MagicMock()
    db.messages = MagicMock(
        find_one=AsyncMock(return_value={"direction": "inbound", "provider": "twilio"}),
        update_one=AsyncMock(),
    )
    with (
        patch.object(messages_route.lead_service, "get_lead", new=AsyncMock(return_value=lead)),
        patch.object(messages_route.message_service, "insert_message", new=insert),
        patch.object(messages_route, "enqueue", enqueue),
        patch("app.security.rate_limit.rate_limit_send"),
        patch("app.services.whatsapp_eligibility._sender_ok", return_value=True),
        patch.object(messages_route, "get_db", return_value=db),
        patch.object(
            messages_route,
            "get_approved_template",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=400,
                    detail="Meta templates cannot be sent as Twilio Content templates",
                )
            ),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await messages_route.send_message(
            MessageSend(
                lead_id=str(lead["_id"]),
                template_id=str(tmpl["_id"]),
                message_purpose="transactional",
            ),
            user=user,
        )
    assert exc.value.status_code == 400
    assert "Meta templates cannot be sent as Twilio Content" in str(exc.value.detail)
    insert.assert_not_called()
    enqueue.assert_not_called()

    mem = _mem()
    mem.templates.docs.append(tmpl)
    with patch.object(templates_route, "get_db", return_value=mem), pytest.raises(HTTPException) as exc2:
        await templates_route.get_approved_template(str(user["_id"]), str(tmpl["_id"]))
    assert exc2.value.status_code == 400


def test_worker_meta_template_calls_helper_not_twilio():
    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    message_id = str(ObjectId())
    tmpl = _approved_meta_tmpl(user_id)
    queued = {
        "_id": ObjectId(message_id),
        "user_id": user_id,
        "provider": "meta",
        "message_type": "template",
        "status": "queued",
        "template_id": str(tmpl["_id"]),
        "meta_template_name": "order_update",
        "meta_language_code": "en_US",
        "content_variables": {"1": "42", "header": "Hi", "button:0": "zz"},
        "message_purpose": "transactional",
    }
    sent = {**queued, "status": "sent", "provider_message_id": "wamid.T"}
    db = MagicMock()
    db.messages.find_one = MagicMock(side_effect=[queued, sent])
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, ObjectId(lead_id)))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "meta_phone_number_id": PNID})
    db.templates.find_one = MagicMock(return_value=tmpl)
    db.messages.update_one = MagicMock()
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(
            tasks,
            "get_whatsapp_send_eligibility",
            return_value=SimpleNamespace(
                allowed=True,
                reason_code="ok",
                safe_message="",
                consent_status="opted_in",
                window_status="closed",
            ),
        ),
        patch.object(
            tasks,
            "send_whatsapp_template",
            return_value={"provider": "meta", "provider_message_id": "wamid.T", "status": "sent"},
        ) as send_tpl,
        patch.object(tasks, "send_whatsapp_text") as send_text,
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "_publish"),
    ):
        tasks.send_outbound_message(message_id, user_id, lead_id, body=None)
    send_tpl.assert_called_once()
    assert send_tpl.call_args.kwargs["provider"] == "meta"
    assert send_tpl.call_args.kwargs["name"] == "order_update"
    send_text.assert_not_called()
    twilio.assert_not_called()
    fields = db.messages.update_one.call_args[0][1]["$set"]
    assert fields["provider_message_id"] == "wamid.T"
    assert "twilio_sid" not in fields


def test_worker_graph_failure_no_twilio_fallback():
    from app.services.meta_whatsapp_service import MetaWhatsAppError

    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    message_id = str(ObjectId())
    tmpl = _approved_meta_tmpl(user_id)
    queued = {
        "_id": ObjectId(message_id),
        "user_id": user_id,
        "provider": "meta",
        "message_type": "template",
        "status": "queued",
        "template_id": str(tmpl["_id"]),
        "meta_template_name": "order_update",
        "meta_language_code": "en_US",
        "content_variables": {"1": "42", "header": "Hi", "button:0": "zz"},
        "message_purpose": "transactional",
    }
    failed = {**queued, "status": "failed"}
    db = MagicMock()
    db.messages.find_one = MagicMock(side_effect=[queued, failed])
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, ObjectId(lead_id)))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id)})
    db.templates.find_one = MagicMock(return_value=tmpl)
    db.messages.update_one = MagicMock()
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(
            tasks,
            "get_whatsapp_send_eligibility",
            return_value=SimpleNamespace(
                allowed=True,
                reason_code="ok",
                safe_message="",
                consent_status="opted_in",
                window_status="closed",
            ),
        ),
        patch.object(
            tasks,
            "send_whatsapp_template",
            side_effect=MetaWhatsAppError("nope", status_code=400),
        ),
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "_publish"),
    ):
        tasks.send_outbound_message(message_id, user_id, lead_id, body=None)
    twilio.assert_not_called()
    assert db.messages.update_one.call_args[0][1]["$set"]["status"] == "failed"


@pytest.mark.asyncio
async def test_status_2d_matches_template_wamid():
    mem = _mem()
    uid = str(ObjectId())
    mem.users.docs.append({"_id": ObjectId(uid), "meta_phone_number_id": PNID})
    mid = ObjectId()
    mem.messages.docs.append(
        {
            "_id": mid,
            "user_id": uid,
            "direction": "outbound",
            "provider": "meta",
            "provider_message_id": "wamid.TPL",
            "status": "sent",
            "message_type": "template",
        }
    )
    with patch("app.config.settings.META_PHONE_NUMBER_ID", PNID):
        await apply_meta_status_update(
            provider_message_id="wamid.TPL",
            status_raw="delivered",
            errors=[],
            phone_number_id=PNID,
            db=mem,
        )
    assert mem.messages.docs[0]["status"] == "delivered"


@pytest.mark.asyncio
async def test_media_header_template_blocked_on_send():
    user = {"_id": ObjectId(), "meta_phone_number_id": PNID}
    lead = _open_lead(str(user["_id"]), ObjectId())
    tmpl = _approved_meta_tmpl(str(user["_id"]))
    tmpl["send_supported"] = False
    tmpl["send_unsupported_reason"] = "Media header templates are not supported in this version."
    with pytest.raises(HTTPException) as exc:
        await _send_meta_tpl(lead, user, tmpl, {})
    assert exc.value.status_code == 400
    assert "cannot be sent" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_tenant_isolation_get_sendable():
    a = str(ObjectId())
    b = str(ObjectId())
    tmpl = _approved_meta_tmpl(a)
    mem = _mem()
    mem.templates.docs.append(tmpl)
    mem.users.docs.append({"_id": ObjectId(b), "meta_phone_number_id": PNID})
    with (
        patch.object(templates_route, "get_db", return_value=mem),
        patch.object(mt.settings, "META_ACCESS_TOKEN", TOKEN),
        patch.object(mt.settings, "META_WABA_ID", WABA),
        patch.object(mt.settings, "META_PHONE_NUMBER_ID", PNID),
        pytest.raises(HTTPException) as exc,
    ):
        await templates_route.get_sendable_meta_template(b, str(tmpl["_id"]))
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_list_provider_filter_and_invalid():
    uid = str(ObjectId())
    mem = _mem()
    mem.templates.docs.append(_approved_meta_tmpl(uid))
    mem.templates.docs.append(
        {
            "_id": ObjectId(),
            "user_id": uid,
            "provider": "twilio_content",
            "name": "HX Welcome",
            "content_sid": "HXabc",
            "status": "approved",
        }
    )
    with patch.object(templates_route, "get_db", return_value=mem):
        meta_rows = await templates_route.list_templates(
            user={"_id": ObjectId(uid)},
            status=None,
            whatsapp_status=None,
            q=None,
            refresh=False,
            provider="meta",
        )
        with pytest.raises(HTTPException) as exc:
            await templates_route.list_templates(
                user={"_id": ObjectId(uid)},
                status=None,
                whatsapp_status=None,
                q=None,
                refresh=False,
                provider="sms",
            )
    assert exc.value.status_code == 400
    assert len(meta_rows) == 1
    assert meta_rows[0].get("provider") == "meta"


def test_missing_language_blocked():
    tmpl = _approved_meta_tmpl("u")
    tmpl["meta_language_code"] = ""
    assert mt.is_meta_template_sendable(tmpl) is False
    with pytest.raises(mt.MetaTemplateError):
        mt.build_graph_components(
            template=tmpl,
            content_variables={"1": "1", "header": "h", "button:0": "b"},
        )


def test_retry_stays_meta():
    from app.services.meta_whatsapp_service import MetaWhatsAppError

    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    message_id = str(ObjectId())
    tmpl = _approved_meta_tmpl(user_id)
    queued = {
        "_id": ObjectId(message_id),
        "user_id": user_id,
        "provider": "meta",
        "message_type": "template",
        "status": "queued",
        "template_id": str(tmpl["_id"]),
        "meta_template_name": "order_update",
        "meta_language_code": "en_US",
        "content_variables": {"1": "42", "header": "Hi", "button:0": "zz"},
        "message_purpose": "transactional",
    }
    db = MagicMock()
    db.messages.find_one = MagicMock(return_value=queued)
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, ObjectId(lead_id)))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "meta_phone_number_id": PNID})
    db.templates.find_one = MagicMock(return_value=tmpl)
    db.messages.update_one = MagicMock()
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(tasks, "max_retries", return_value=3),
        patch.object(
            tasks,
            "get_whatsapp_send_eligibility",
            return_value=SimpleNamespace(
                allowed=True,
                reason_code="ok",
                safe_message="",
                consent_status="opted_in",
                window_status="closed",
            ),
        ),
        patch.object(
            tasks,
            "send_whatsapp_template",
            side_effect=MetaWhatsAppError("rate", status_code=429),
        ),
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "_schedule_message_retry", return_value=True) as retry,
        patch.object(tasks, "_publish"),
    ):
        tasks.send_outbound_message(message_id, user_id, lead_id, body=None)
    twilio.assert_not_called()
    retry.assert_called_once()
    assert retry.call_args.args[1] == user_id
    assert retry.call_args.kwargs.get("content_sid") in (None, "")
    set_status = db.messages.update_one.call_args_list[-1][0][1]["$set"]["status"]
    assert set_status == "queued"
