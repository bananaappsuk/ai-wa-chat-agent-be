"""Phase 2A Meta inbound → CRM / Live Chat tests (no Graph, no real Meta)."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from contextlib import ExitStack, asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.main import app
from app.services import meta_whatsapp_service
from app.services.inbound_whatsapp import InboundMessage, process_inbound_message
from app.workers import ai_tasks, tasks


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _meta_payload(
    *,
    phone_number_id: str,
    wamid: str,
    from_wa: str = "447700900123",
    body: str = "hello meta",
    msg_type: str = "text",
    display_phone_number: str = "15550001111",
) -> dict:
    msg: dict = {
        "from": from_wa,
        "id": wamid,
        "timestamp": "1710000000",
        "type": msg_type,
    }
    if msg_type == "text":
        msg["text"] = {"body": body}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": display_phone_number,
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Test"}, "wa_id": from_wa}],
                            "messages": [msg],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _match(doc: dict, query: dict) -> bool:
    if not query:
        return False
    for k, v in query.items():
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
                return False
            continue
        if isinstance(v, dict) and "$gte" in v:
            if doc.get(k) is None or doc.get(k) < v["$gte"]:
                return False
            continue
        if doc.get(k) != v:
            return False
    return True


class MemColl:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    async def find_one(self, query=None, projection=None, **kwargs):
        query = query or {}
        if not query:
            return None
        matches = [copy.deepcopy(d) for d in self.docs if _match(d, query)]
        sort = kwargs.get("sort")
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda d: d.get(key) or 0, reverse=direction == -1)
        return matches[0] if matches else None

    async def insert_one(self, doc):
        d = copy.deepcopy(doc)
        d.setdefault("_id", ObjectId())
        self.docs.append(d)
        return SimpleNamespace(inserted_id=d["_id"])

    async def update_one(self, query, update, upsert=False):
        for d in self.docs:
            if _match(d, query):
                if "$set" in update:
                    d.update(update["$set"])
                if "$inc" in update:
                    for k, v in update["$inc"].items():
                        d[k] = (d.get(k) or 0) + v
                return SimpleNamespace(matched_count=1, modified_count=1)
        if upsert:
            created = dict(query)
            created.setdefault("_id", ObjectId())
            if "$set" in update:
                created.update(update["$set"])
            self.docs.append(created)
            return SimpleNamespace(matched_count=0, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)

    async def update_many(self, query, update):
        n = 0
        for d in self.docs:
            if _match(d, query):
                if "$set" in update:
                    d.update(update["$set"])
                n += 1
        return SimpleNamespace(modified_count=n)

    async def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if _match(d, query):
                self.docs.pop(i)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class MemDB:
    def __init__(self) -> None:
        self.users = MemColl()
        self.leads = MemColl()
        self.messages = MemColl()
        self.webhook_events = MemColl()
        self.blacklist = MemColl()
        self.campaign_recipients = MemColl()
        self.campaigns = MemColl()
        self.consent_events = MemColl()
        self.activity_events = MemColl()
        self.notifications = MemColl()


def _user(pnid: str) -> dict:
    return {"_id": ObjectId(), "meta_phone_number_id": pnid, "email": f"{pnid}@example.com"}


@pytest.fixture
def pushes():
    items: list[tuple] = []

    async def _push(user_id, event, data):
        items.append((str(user_id), event, data))

    return items, _push


@pytest.fixture
def mem():
    return MemDB()


@contextmanager
def patched_meta(mem: MemDB, push, send=None, enqueue=None):
    send = send if send is not None else MagicMock()
    enqueue = enqueue if enqueue is not None else MagicMock()
    with ExitStack() as stack:
        stack.enter_context(patch("app.routes.meta_webhook.get_db", return_value=mem))
        stack.enter_context(patch("app.services.lead_service.get_db", return_value=mem))
        stack.enter_context(patch("app.services.lead_scoring.get_db", return_value=mem))
        stack.enter_context(patch("app.services.inbound_whatsapp.ws_manager.push", new=push))
        stack.enter_context(patch("app.services.notifications.create_notification", new=AsyncMock()))
        stack.enter_context(patch("app.security.rate_limit.rate_limit_webhook"))
        stack.enter_context(patch("app.services.twilio_service.send_whatsapp", send))
        stack.enter_context(patch("app.workers.queue.enqueue", enqueue))
        yield send, enqueue


def _post_meta(client, payload: dict, secret: str = "app-secret"):
    body = json.dumps(payload).encode("utf-8")
    with patch.object(meta_whatsapp_service.settings, "META_WEBHOOK_VALIDATE_SIGNATURE", True), patch.object(
        meta_whatsapp_service.settings, "META_APP_SECRET", secret
    ):
        return client.post(
            "/api/webhook/meta/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body, secret),
            },
        )


def test_known_phone_number_id_resolves_tenant(client, mem, pushes):
    items, push = pushes
    user = _user("PN_A")
    mem.users.docs.append(user)
    with patched_meta(mem, push):
        res = _post_meta(client, _meta_payload(phone_number_id="PN_A", wamid="wamid.T1"))
    assert res.status_code == 200
    assert mem.messages.docs[0]["user_id"] == str(user["_id"])


def test_unknown_phone_number_id_no_cross_tenant_writes(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_OTHER"))
    queries: list = []
    orig = mem.users.find_one

    async def tracked(query=None, projection=None, **kwargs):
        queries.append(query)
        assert query, "must not query users with empty filter"
        return await orig(query, projection, **kwargs)

    mem.users.find_one = tracked  # type: ignore[method-assign]
    with patched_meta(mem, push):
        res = _post_meta(client, _meta_payload(phone_number_id="PN_UNKNOWN", wamid="wamid.U1"))
    assert res.status_code == 200
    assert mem.leads.docs == []
    assert mem.messages.docs == []
    assert items == []
    assert all((q or {}).get("meta_phone_number_id") == "PN_UNKNOWN" for q in queries)


def test_new_sender_creates_lead_and_persists_meta_message(client, mem, pushes):
    items, push = pushes
    user = _user("PN_A")
    mem.users.docs.append(user)
    with patched_meta(mem, push):
        res = _post_meta(
            client, _meta_payload(phone_number_id="PN_A", wamid="wamid.NEW1", body="hello meta")
        )
    assert res.status_code == 200
    assert len(mem.leads.docs) == 1
    assert mem.leads.docs[0]["phone"] == "+447700900123"
    assert mem.leads.docs[0]["source"] == "whatsapp"
    msg = mem.messages.docs[0]
    assert msg["direction"] == "inbound"
    assert msg["status"] == "received"
    assert msg["message"] == "hello meta"
    assert msg["provider"] == "meta"
    assert msg["provider_message_id"] == "wamid.NEW1"
    assert mem.leads.docs[0].get("last_inbound_at")
    assert mem.leads.docs[0].get("whatsapp_window_expires_at")
    events = [e for _, e, _ in items]
    assert "message:new" in events
    assert "lead:updated" in events


def test_existing_lead_reused(client, mem, pushes):
    items, push = pushes
    user = _user("PN_A")
    mem.users.docs.append(user)
    lead_id = ObjectId()
    mem.leads.docs.append(
        {
            "_id": lead_id,
            "user_id": str(user["_id"]),
            "phone": "+447700900123",
            "source": "whatsapp",
        }
    )
    with patched_meta(mem, push):
        _post_meta(client, _meta_payload(phone_number_id="PN_A", wamid="wamid.REUSE1"))
    assert len(mem.leads.docs) == 1
    assert mem.messages.docs[0]["lead_id"] == str(lead_id)


def test_stop_opts_out_without_twilio_send(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    send = MagicMock()
    with patched_meta(mem, push, send=send):
        res = _post_meta(client, _meta_payload(phone_number_id="PN_A", wamid="wamid.STOP1", body="STOP"))
    assert res.status_code == 200
    lead = mem.leads.docs[0]
    assert lead["whatsapp_consent_status"] == "opted_out"
    assert lead.get("blacklisted") is True
    assert mem.blacklist.docs
    send.assert_not_called()
    assert [m for m in mem.messages.docs if m.get("direction") == "outbound"] == []


def test_start_restores_consent(client, mem, pushes):
    items, push = pushes
    user = _user("PN_A")
    uid = str(user["_id"])
    mem.users.docs.append(user)
    mem.leads.docs.append(
        {
            "_id": ObjectId(),
            "user_id": uid,
            "phone": "+447700900123",
            "whatsapp_consent_status": "opted_out",
            "blacklisted": True,
        }
    )
    mem.blacklist.docs.append({"user_id": uid, "phone": "+447700900123"})
    with patched_meta(mem, push):
        _post_meta(client, _meta_payload(phone_number_id="PN_A", wamid="wamid.START1", body="START"))
    lead = mem.leads.docs[0]
    assert lead["whatsapp_consent_status"] == "opted_in"
    assert mem.blacklist.docs == []


def test_duplicate_wamid_one_message(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    payload = _meta_payload(phone_number_id="PN_A", wamid="wamid.DUP1")
    with patched_meta(mem, push):
        assert _post_meta(client, payload).status_code == 200
        assert _post_meta(client, payload).status_code == 200
    assert len(mem.messages.docs) == 1
    assert len([e for _, e, _ in items if e == "message:new"]) == 1


def test_meta_does_not_enqueue_ai_welcome_or_classifier(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    enq = MagicMock()
    with patched_meta(mem, push, enqueue=enq):
        _post_meta(client, _meta_payload(phone_number_id="PN_A", wamid="wamid.NOAI"))
    assert enq.call_count == 0
    for call in enq.call_args_list:
        fn = call.args[0] if call.args else None
        assert fn is not tasks.generate_and_send_ai_reply
        assert fn is not tasks.send_welcome_and_terms
        assert fn is not ai_tasks.classify_latest_inbound


def test_two_tenants_isolated(client, mem, pushes):
    items, push = pushes
    a = _user("PN_A")
    b = _user("PN_B")
    mem.users.docs.extend([a, b])
    with patched_meta(mem, push):
        _post_meta(client, _meta_payload(phone_number_id="PN_A", wamid="wamid.A1", from_wa="447700900111"))
        _post_meta(client, _meta_payload(phone_number_id="PN_B", wamid="wamid.B1", from_wa="447700900222"))
    msg_a = next(m for m in mem.messages.docs if m["provider_message_id"] == "wamid.A1")
    msg_b = next(m for m in mem.messages.docs if m["provider_message_id"] == "wamid.B1")
    assert msg_a["user_id"] == str(a["_id"])
    assert msg_b["user_id"] == str(b["_id"])
    assert msg_a["user_id"] != msg_b["user_id"]


@pytest.mark.asyncio
async def test_missing_wamid_not_persisted(mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    with patch("app.services.inbound_whatsapp.ws_manager.push", new=push):
        result = await process_inbound_message(
            InboundMessage(
                provider="meta",
                provider_message_id="",
                customer_phone="+447700900123",
                business_identifier="PN_A",
                body="hi",
                profile_name=None,
                timestamp=None,
                message_type="text",
                skip_provider_outbound=True,
                skip_ai_jobs=True,
            ),
            db=mem,
        )
    assert result.outcome == "missing_id"
    assert mem.messages.docs == []
    assert items == []


def test_no_first_user_fallback(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append({"_id": ObjectId(), "role": "admin", "email": "admin@example.com"})
    orig = mem.users.find_one

    async def guarded(query=None, projection=None, **kwargs):
        if not query:
            pytest.fail("empty user query is forbidden")
        if "meta_phone_number_id" not in query:
            pytest.fail(f"unexpected user query {query}")
        return await orig(query, projection, **kwargs)

    mem.users.find_one = guarded  # type: ignore[method-assign]
    with patched_meta(mem, push):
        assert _post_meta(client, _meta_payload(phone_number_id="PN_NONE", wamid="wamid.NONE")).status_code == 200
    assert mem.messages.docs == []
    assert mem.leads.docs == []
