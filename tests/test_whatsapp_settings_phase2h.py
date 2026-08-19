"""Phase 2H WhatsApp integration settings (no live Twilio/Meta)."""
from __future__ import annotations

import copy
import inspect
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.main import app
from app.middleware.auth import create_access_token, hash_password
from app.services.campaign_provider import stored_provider
from app.services.whatsapp_settings import build_whatsapp_settings


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def _match(doc: dict, query: dict) -> bool:
    if not query:
        return False
    for k, v in query.items():
        if isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
            continue
        if isinstance(v, dict) and "$in" in v:
            if doc.get(k) not in v["$in"]:
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
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)


class MemDB:
    def __init__(self) -> None:
        self.users = MemColl()
        self.activity_events = MemColl()


def _user(**kw):
    uid = kw.pop("_id", ObjectId())
    base = {
        "_id": uid,
        "email": f"{uid}@example.com",
        "password_hash": hash_password("Password1"),
        "full_name": "U",
        "role": "user",
        "plan": "free",
        "banned": False,
        "active": True,
    }
    base.update(kw)
    return base


_PLATFORM = {
    "environment": "development",
    "sender_configured": True,
    "sender_type": "direct",
    "status_callback_configured": True,
    "warnings": [],
}


@contextmanager
def _settings_ctx(mem: MemDB, **meta_env):
    with (
        patch("app.middleware.auth.get_db", return_value=mem),
        patch("app.routes.settings.get_db", return_value=mem),
        patch(
            "app.services.whatsapp_settings.whatsapp_status_payload",
            return_value=_PLATFORM,
        ),
        patch("app.services.whatsapp_settings.detect_sender_type", return_value="direct"),
        patch.object(
            __import__("app.services.whatsapp_settings", fromlist=["settings"]).settings,
            "META_ACCESS_TOKEN",
            meta_env.get("token", "tok"),
        ),
        patch.object(
            __import__("app.services.whatsapp_settings", fromlist=["settings"]).settings,
            "META_PHONE_NUMBER_ID",
            meta_env.get("pnid", "123456789012345"),
        ),
        patch.object(
            __import__("app.services.whatsapp_settings", fromlist=["settings"]).settings,
            "META_WABA_ID",
            meta_env.get("waba", "555555555555555"),
        ),
    ):
        yield


def _auth(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user['_id']), 'user')}"}


def test_get_requires_auth(client):
    res = client.get("/api/settings/whatsapp")
    assert res.status_code == 401


def test_get_only_current_tenant(client):
    mem = MemDB()
    a = _user(meta_phone_number_id="123456789012345", twilio_whatsapp_to="+447700900001")
    b = _user(meta_phone_number_id="999999999999999", twilio_whatsapp_to="+447700900002")
    mem.users.docs.extend([a, b])
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(a))
    assert res.status_code == 200
    body = res.json()
    assert body["meta"]["phone_number_id"] == "123456789012345"
    assert body["twilio"]["routing_number"] == "+447700900001"
    assert "999999999999999" not in str(body)
    assert "+447700900002" not in str(body)


def _assert_no_secrets(body: dict):
    blob = str(body).lower()
    for needle in (
        "access_token",
        "app_secret",
        "auth_token",
        "account_sid",
        "webhook_verify_token",
        "meta_access_token",
        "twilio_auth_token",
        "jwt_secret",
    ):
        assert needle not in blob
    assert "sk-" not in blob


def test_get_has_no_meta_access_token_or_app_secret(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(user))
    assert res.status_code == 200
    _assert_no_secrets(res.json())
    assert "META_ACCESS_TOKEN" not in str(res.json())
    assert "META_APP_SECRET" not in str(res.json())


def test_get_has_no_twilio_auth_token(client):
    mem = MemDB()
    user = _user(twilio_whatsapp_to="+447700900010")
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(user))
    assert res.status_code == 200
    _assert_no_secrets(res.json())
    assert "TWILIO_AUTH_TOKEN" not in str(res.json())
    assert "TWILIO_ACCOUNT_SID" not in str(res.json())


def test_twilio_only_tenant_state(client):
    mem = MemDB()
    user = _user(twilio_whatsapp_to="+447700900011")
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(user))
    body = res.json()
    assert body["twilio"]["configured"] is True
    assert body["twilio"]["routing_ready"] is True
    assert body["twilio"]["status"] == "connected"
    assert body["meta"]["configured"] is False
    assert body["meta"]["status"] == "not_configured"


def test_meta_only_tenant_state(client):
    mem = MemDB()
    user = _user(
        meta_phone_number_id="123456789012345",
        meta_waba_id="555555555555555",
        meta_display_phone_number="+15550001111",
    )
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(user))
    body = res.json()
    assert body["meta"]["configured"] is True
    assert body["meta"]["sending_ready"] is True
    assert body["meta"]["poc_aligned"] is True
    assert body["meta"]["status"] == "connected"
    assert body["twilio"]["configured"] is False


def test_both_provider_tenant_state(client):
    mem = MemDB()
    user = _user(
        twilio_whatsapp_to="+447700900012",
        meta_phone_number_id="123456789012345",
    )
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(user))
    body = res.json()
    assert body["twilio"]["configured"] is True
    assert body["meta"]["configured"] is True


def test_neither_provider_state(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(user))
    body = res.json()
    assert body["twilio"]["status"] == "not_configured"
    assert body["meta"]["status"] == "not_configured"
    assert not body["meta"]["phone_number_id"]
    assert not body["meta"]["waba_id"]


def test_meta_identifiers_returned_safely(client):
    mem = MemDB()
    user = _user(
        meta_phone_number_id="123456789012345",
        meta_waba_id="555555555555555",
        meta_display_phone_number="+15550001111",
    )
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(user))
    meta = res.json()["meta"]
    assert meta["phone_number_id"] == "123456789012345"
    assert meta["waba_id"] == "555555555555555"
    assert meta["display_phone_number"] == "+15550001111"
    _assert_no_secrets(res.json())


def test_patch_twilio_routing_and_normalization(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"twilio": {"routing_number": "whatsapp:+447700900020"}},
        )
    assert res.status_code == 200
    assert res.json()["twilio"]["routing_number"] == "+447700900020"
    stored = mem.users.docs[0]["twilio_whatsapp_to"]
    assert stored == "+447700900020"


def test_invalid_twilio_number_400(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"twilio": {"routing_number": "not-a-phone"}},
        )
    assert res.status_code == 400


def test_duplicate_twilio_number_409(client):
    mem = MemDB()
    a = _user(twilio_whatsapp_to="+447700900030")
    b = _user()
    mem.users.docs.extend([a, b])
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(b),
            json={"twilio": {"routing_number": "+447700900030"}},
        )
    assert res.status_code == 409
    assert "already linked" in res.json()["detail"].lower()
    assert str(a["_id"]) not in str(res.json())


def test_patch_meta_pnid(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={
                "meta": {
                    "phone_number_id": "123456789012345",
                    "waba_id": "555555555555555",
                    "display_phone_number": "+15550002222",
                }
            },
        )
    assert res.status_code == 200
    body = res.json()["meta"]
    assert body["phone_number_id"] == "123456789012345"
    assert body["waba_id"] == "555555555555555"
    assert body["display_phone_number"] == "+15550002222"


def test_wrong_poc_meta_pnid_rejected(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"meta": {"phone_number_id": "999999999999999"}},
        )
    assert res.status_code == 400
    assert mem.users.docs[0].get("meta_phone_number_id") is None


def test_duplicate_meta_pnid_409(client):
    mem = MemDB()
    a = _user(meta_phone_number_id="123456789012345")
    b = _user()
    mem.users.docs.extend([a, b])
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(b),
            json={"meta": {"phone_number_id": "123456789012345"}},
        )
    assert res.status_code == 409
    assert str(a["_id"]) not in str(res.json())


def test_invalid_meta_pnid_400(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"meta": {"phone_number_id": "abc"}},
        )
    assert res.status_code == 400


def test_invalid_display_number_400(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"meta": {"display_phone_number": "abc"}},
        )
    assert res.status_code == 400


def test_patch_updates_only_current_user(client):
    mem = MemDB()
    a = _user()
    b = _user(twilio_whatsapp_to="+447700900040")
    mem.users.docs.extend([a, b])
    with _settings_ctx(mem):
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(a),
            json={"twilio": {"routing_number": "+447700900041"}},
        )
    assert res.status_code == 200
    assert mem.users.docs[0]["twilio_whatsapp_to"] == "+447700900041"
    assert mem.users.docs[1]["twilio_whatsapp_to"] == "+447700900040"


def test_unknown_secret_and_provider_fields_rejected(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        secret = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"access_token": "secret-token", "twilio": {"routing_number": "+447700900050"}},
        )
        provider = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"provider": "meta"},
        )
        extra = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"twilio": {"auth_token": "x"}},
        )
    assert secret.status_code == 400
    assert provider.status_code == 400
    assert extra.status_code in (400, 422)
    assert mem.users.docs[0].get("twilio_whatsapp_to") is None


def test_audit_activity_without_secrets(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with _settings_ctx(mem), patch("app.routes.settings.audit") as aud:
        res = client.patch(
            "/api/settings/whatsapp",
            headers=_auth(user),
            json={"twilio": {"routing_number": "+447700900060"}},
        )
    assert res.status_code == 200
    aud.assert_called()
    args, kwargs = aud.call_args
    assert args[0] == "settings.whatsapp_update"
    extra = str(kwargs.get("extra") or {})
    assert "Password1" not in extra
    assert "tok" not in extra
    assert "auth_token" not in extra.lower()
    events = mem.activity_events.docs
    assert events
    assert events[0]["event_type"] == "settings.whatsapp_updated"
    assert "token" not in str(events[0]).lower() or "webhook" in str(events[0]).lower()
    blob = str(events[0]).lower()
    assert "access_token" not in blob
    assert "auth_token" not in blob


def test_runtime_providers_unaffected():
    assert stored_provider({"provider": "meta"}) == "meta"
    assert stored_provider({"provider": "twilio"}) == "twilio"
    assert stored_provider({"provider": None}) == "twilio"
    import app.services.inbound_whatsapp as inbound
    import app.services.whatsapp_outbound as outbound
    import app.services.campaign_provider as campaign_provider
    import app.workers.tasks as tasks

    for mod in (inbound, outbound, campaign_provider, tasks):
        src = inspect.getsource(mod)
        assert "whatsapp_settings" not in src
        assert "user.whatsapp_provider" not in src


def test_get_webhook_ready_unknown(client):
    mem = MemDB()
    user = _user(meta_phone_number_id="123456789012345")
    mem.users.docs.append(user)
    with _settings_ctx(mem):
        res = client.get("/api/settings/whatsapp", headers=_auth(user))
    assert res.json()["meta"]["webhook_ready"] is None


def test_legacy_whatsapp_status_contract(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with (
        patch("app.middleware.auth.get_db", return_value=mem),
        patch("app.routes.settings.get_db", return_value=mem),
    ):
        res = client.get("/api/settings/whatsapp-status", headers=_auth(user))
    assert res.status_code == 200
    body = res.json()
    assert "sender_configured" in body
    assert "warnings" in body
    assert "phone_number_id" not in body


def test_builder_misaligned_meta_requires_action():
    user = {
        "meta_phone_number_id": "111111111111111",
    }
    with (
        patch.object(
            __import__("app.services.whatsapp_settings", fromlist=["settings"]).settings,
            "META_PHONE_NUMBER_ID",
            "123456789012345",
        ),
        patch.object(
            __import__("app.services.whatsapp_settings", fromlist=["settings"]).settings,
            "META_ACCESS_TOKEN",
            "tok",
        ),
        patch(
            "app.services.whatsapp_settings.whatsapp_status_payload",
            return_value=_PLATFORM,
        ),
    ):
        out = build_whatsapp_settings(user)
    assert out["meta"]["status"] == "requires_action"
    assert out["meta"]["poc_aligned"] is False
    assert out["meta"]["sending_ready"] is False
