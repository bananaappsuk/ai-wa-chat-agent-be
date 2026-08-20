"""Phase 2I-B Meta Embedded Signup onboarding (no live Graph/Twilio)."""
from __future__ import annotations

import copy
import inspect
import json
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pymongo.errors import DuplicateKeyError

from app.main import app
from app.middleware.auth import create_access_token, hash_password
from app.services.campaign_provider import stored_provider
from app.services.meta_credentials import (
    MetaCredentialsError,
    decrypt_secret,
    get_meta_credentials_for_user,
    upsert_encrypted_access_token,
)
from app.services.meta_onboarding import (
    SESSION_PREFIX,
    assert_session_for_user,
    complete_onboarding,
    disconnect_meta,
    exchange_authorization_code,
    start_onboarding_session,
)
from app.services.status_callback import apply_meta_status_update
from app.services.whatsapp_settings import build_whatsapp_settings


KEY = "k" * 32
APP_ID = "111222333444555"
APP_SECRET = "test-app-secret-not-real"
CONFIG_ID = "embedded-signup-config-test"
TOKEN = "EAA_test_tenant_access_token_not_real"
CODE = "AQB_test_authorization_code_not_real"
WABA = "555555555555555"
PN_A = "111111111111111"
PN_B = "222222222222222"
PN_FAKE = "999999999999999"


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def ttl(self, key):
        return 600 if key in self.store else -2

    def delete(self, key):
        self.store.pop(key, None)
        return 1


class FakeResp:
    def __init__(self, status_code: int, data: dict):
        self.status_code = status_code
        self._data = data
        self.is_error = status_code >= 400

    def json(self):
        return self._data


class FakeGraphClient:
    def __init__(self, router: "GraphRouter"):
        self.router = router

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None, params=None):
        return self.router.handle("GET", url, headers=headers, params=params, json_body=None)

    def post(self, url, headers=None, json=None):
        return self.router.handle("POST", url, headers=headers, params=None, json_body=json)


class GraphRouter:
    def __init__(
        self,
        *,
        waba=WABA,
        pnid=PN_A,
        display="+15550001111",
        subscribe_ok=True,
        register_ok=True,
        phones=None,
        exchange_ok=True,
    ):
        self.waba = waba
        self.pnid = pnid
        self.display = display
        self.subscribe_ok = subscribe_ok
        self.register_ok = register_ok
        self.phones = phones
        self.exchange_ok = exchange_ok
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        return FakeGraphClient(self)

    def handle(self, method, url, headers=None, params=None, json_body=None):
        self.calls.append((method, url, params, json_body, headers))
        path = str(url).split("facebook.com/")[-1]
        params = params or {}
        if "oauth/access_token" in path:
            if not self.exchange_ok:
                return FakeResp(400, {"error": {"message": "bad code"}})
            assert params.get("client_id") == APP_ID
            assert params.get("client_secret") == APP_SECRET
            assert params.get("code") == CODE
            return FakeResp(200, {"access_token": TOKEN, "token_type": "bearer", "expires_in": 3600})
        if path.endswith(f"{self.waba}/phone_numbers") or f"/{self.waba}/phone_numbers" in path:
            rows = self.phones if self.phones is not None else [
                {"id": self.pnid, "display_phone_number": self.display, "verified_name": "Test"}
            ]
            return FakeResp(200, {"data": rows})
        if path.endswith(self.waba) or path.endswith(f"/{self.waba}"):
            return FakeResp(200, {"id": self.waba, "name": "Test WABA"})
        if method == "POST" and "subscribed_apps" in path:
            if self.subscribe_ok:
                return FakeResp(200, {"success": True})
            return FakeResp(400, {"error": {"message": "subscribe failed", "code": 100}})
        if method == "POST" and path.endswith("/register"):
            if self.register_ok:
                return FakeResp(200, {"success": True})
            return FakeResp(400, {"error": {"message": "register failed", "code": 100}})
        return FakeResp(404, {"error": {"message": "unknown"}})


class SyncColl:
    def __init__(self) -> None:
        self.docs: list[dict] = []

    def find_one(self, query=None, **kwargs):
        query = query or {}
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                return copy.deepcopy(d)
        return None

    def insert_one(self, doc):
        d = copy.deepcopy(doc)
        d.setdefault("_id", ObjectId())
        self.docs.append(d)
        return SimpleNamespace(inserted_id=d["_id"])

    def update_one(self, query, update):
        for d in self.docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set") or {})
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    def delete_one(self, query):
        for i, d in enumerate(self.docs):
            if all(d.get(k) == v for k, v in query.items()):
                self.docs.pop(i)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class SyncDB:
    def __init__(self) -> None:
        self.meta_credentials = SyncColl()


def _match(doc: dict, query: dict) -> bool:
    if not query:
        return False
    for k, v in query.items():
        if isinstance(v, dict) and "$ne" in v:
            if doc.get(k) == v["$ne"]:
                return False
            continue
        if doc.get(k) != v:
            return False
    return True


class MemColl:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self.raise_dup = False

    def find(self, query=None, projection=None, **kwargs):
        query = query or {}
        matches = [copy.deepcopy(d) for d in self.docs if not query or _match(d, query)]

        class _Cur:
            def __init__(self, docs):
                self._docs = docs

            async def to_list(self, length=None):
                if length is None:
                    return list(self._docs)
                return list(self._docs[: int(length)])

        return _Cur(matches)

    async def find_one(self, query=None, projection=None, **kwargs):
        query = query or {}
        matches = [copy.deepcopy(d) for d in self.docs if _match(d, query)]
        return matches[0] if matches else None

    async def insert_one(self, doc):
        d = copy.deepcopy(doc)
        d.setdefault("_id", ObjectId())
        self.docs.append(d)
        return SimpleNamespace(inserted_id=d["_id"])

    async def update_one(self, query, update, upsert=False):
        if self.raise_dup:
            raise DuplicateKeyError("E11000 duplicate meta_phone_number_id")
        for d in self.docs:
            if _match(d, query):
                d.update(update.get("$set") or {})
                return SimpleNamespace(matched_count=1, modified_count=1)
        return SimpleNamespace(matched_count=0, modified_count=0)


class MemDB:
    def __init__(self) -> None:
        self.users = MemColl()
        self.activity_events = MemColl()
        self.messages = MemColl()
        self.campaign_recipients = MemColl()
        self.blast_recipients = MemColl()


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
        "twilio_whatsapp_to": "+447700900001",
    }
    base.update(kw)
    return base


def _auth(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user['_id']), 'user')}"}


@contextmanager
def _cfg(**extra):
    from app.config import settings as cfg

    kw = {
        "META_APP_ID": APP_ID,
        "META_APP_SECRET": APP_SECRET,
        "META_EMBEDDED_SIGNUP_CONFIG_ID": CONFIG_ID,
        "META_GRAPH_VERSION": "v21.0",
        "META_TOKEN_ENCRYPTION_KEY": KEY,
        "META_ONBOARDING_STATE_TTL_SECONDS": 600,
        **extra,
    }
    with patch.multiple(cfg, **kw), patch.multiple(
        "app.services.meta_credentials.settings",
        META_TOKEN_ENCRYPTION_KEY=kw["META_TOKEN_ENCRYPTION_KEY"],
        JWT_SECRET="jwt-secret-distinct-from-meta-key",
    ):
        yield


@contextmanager
def _redis_ctx(r: FakeRedis):
    with patch("app.services.meta_onboarding._redis", return_value=r):
        yield r


def _seed_state(r: FakeRedis, *, user_id: str, state: str = "state_" + "A" * 24, consumed: bool = False):
    payload = {"user_id": str(user_id), "nonce": "n1", "created_at": "2026-08-20T00:00:00+00:00", "consumed": consumed}
    r.set(f"{SESSION_PREFIX}{state}", json.dumps(payload), ex=600)
    return state


def _no_secrets(blob) -> None:
    text = str(blob).lower()
    for needle in (TOKEN.lower(), APP_SECRET.lower(), CODE.lower(), "ciphertext"):
        assert needle not in text
    for needle in ("access_token", "app_secret", "client_secret", "meta_access_token"):
        assert needle not in text


# --- start / session ---


def test_start_requires_auth(client):
    res = client.post("/api/settings/whatsapp/meta/onboarding/start", json={})
    assert res.status_code == 401


def test_start_returns_app_id_config_id_not_secret(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    r = FakeRedis()
    with (
        _cfg(),
        _redis_ctx(r),
        patch("app.middleware.auth.get_db", return_value=mem),
        patch("app.routes.settings.get_db", return_value=mem),
        patch("app.routes.settings.audit") as aud,
    ):
        res = client.post("/api/settings/whatsapp/meta/onboarding/start", headers=_auth(user), json={})
    assert res.status_code == 200
    body = res.json()
    assert body["app_id"] == APP_ID
    assert body["config_id"] == CONFIG_ID
    assert body["graph_version"] == "v21.0"
    assert body["state"]
    assert body["state"] in r.store.get(f"{SESSION_PREFIX}{body['state']}", "") or r.get(
        f"{SESSION_PREFIX}{body['state']}"
    )
    _no_secrets(body)
    assert "META_APP_SECRET" not in str(body)
    extra = str((aud.call_args.kwargs or {}).get("extra") or {})
    _no_secrets(extra)
    assert aud.call_args.args[0] == "settings.meta_onboarding_started"


def test_state_random_and_bound_to_user():
    r = FakeRedis()
    with _cfg(), _redis_ctx(r):
        a = start_onboarding_session(user_id="user-a")
        b = start_onboarding_session(user_id="user-b")
    assert a["state"] != b["state"]
    assert len(a["state"]) >= 32
    raw_a = json.loads(r.get(f"{SESSION_PREFIX}{a['state']}"))
    raw_b = json.loads(r.get(f"{SESSION_PREFIX}{b['state']}"))
    assert raw_a["user_id"] == "user-a"
    assert raw_b["user_id"] == "user-b"
    assert raw_a["consumed"] is False


def test_expired_state_rejected():
    r = FakeRedis()
    with _cfg(), _redis_ctx(r):
        with pytest.raises(HTTPException) as ei:
            assert_session_for_user(state="missing-state-value-here", user_id="u1")
    assert ei.value.status_code == 400


def test_consumed_state_rejected():
    r = FakeRedis()
    state = _seed_state(r, user_id="u1", consumed=True)
    with _cfg(), _redis_ctx(r):
        with pytest.raises(HTTPException) as ei:
            assert_session_for_user(state=state, user_id="u1")
    assert ei.value.status_code == 400


def test_tenant_b_cannot_use_tenant_a_state():
    r = FakeRedis()
    state = _seed_state(r, user_id="tenant-a")
    with _cfg(), _redis_ctx(r):
        with pytest.raises(HTTPException) as ei:
            assert_session_for_user(state=state, user_id="tenant-b")
    assert ei.value.status_code == 403


def test_complete_rejects_access_token_field(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with (
        patch("app.middleware.auth.get_db", return_value=mem),
        patch("app.routes.settings.get_db", return_value=mem),
    ):
        res = client.post(
            "/api/settings/whatsapp/meta/onboarding/complete",
            headers=_auth(user),
            json={
                "state": "state_" + "B" * 24,
                "code": CODE,
                "waba_id": WABA,
                "phone_number_id": PN_A,
                "access_token": TOKEN,
            },
        )
    assert res.status_code == 422


def test_complete_requires_code(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with (
        patch("app.middleware.auth.get_db", return_value=mem),
        patch("app.routes.settings.get_db", return_value=mem),
    ):
        res = client.post(
            "/api/settings/whatsapp/meta/onboarding/complete",
            headers=_auth(user),
            json={"state": "state_" + "C" * 24, "waba_id": WABA, "phone_number_id": PN_A},
        )
    assert res.status_code == 422


@pytest.mark.asyncio
async def test_code_exchange_server_side_and_encrypted_store():
    mem = MemDB()
    cred = SyncDB()
    user = _user()
    mem.users.docs.append(user)
    r = FakeRedis()
    state = _seed_state(r, user_id=str(user["_id"]))
    graph = GraphRouter()
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        result = await complete_onboarding(
            mem,
            user=user,
            state=state,
            code=CODE,
            waba_id=WABA,
            phone_number_id=PN_A,
            cred_db=cred,
        )
        row = cred.meta_credentials.docs[0]
        assert TOKEN not in str(row)
        payload = json.loads(decrypt_secret(row["ciphertext"]))
        assert payload["t"] == TOKEN
        assert payload["p"] == PN_A
    assert result["ok"] is True
    assert result["status"] == "connected"
    methods = [c[0] for c in graph.calls]
    assert "GET" in methods
    assert any("oauth/access_token" in str(c[1]) for c in graph.calls)
    assert any("subscribed_apps" in str(c[1]) for c in graph.calls)
    stored = mem.users.docs[0]
    assert TOKEN not in str(stored)
    assert stored["meta_connection_status"] == "connected"
    assert stored["meta_phone_number_id"] == PN_A
    assert stored["meta_waba_id"] == WABA
    assert stored["meta_onboarding_source"] == "embedded_signup"
    assert stored.get("meta_disconnected_at") is None
    consumed = json.loads(r.get(f"{SESSION_PREFIX}{state}"))
    assert consumed["consumed"] is True


def test_exchange_does_not_return_app_secret():
    graph = GraphRouter()
    with _cfg(), patch("app.services.meta_onboarding.httpx.Client", graph):
        out = exchange_authorization_code(CODE)
    assert out["access_token"] == TOKEN
    assert APP_SECRET not in str(out)
    assert "client_secret" not in str(out)


@pytest.mark.asyncio
async def test_unowned_pnid_rejected_and_state_not_consumed():
    mem = MemDB()
    cred = SyncDB()
    user = _user()
    mem.users.docs.append(user)
    r = FakeRedis()
    state = _seed_state(r, user_id=str(user["_id"]))
    graph = GraphRouter(phones=[{"id": PN_B, "display_phone_number": "+15550002222"}])
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        with pytest.raises(HTTPException) as ei:
            await complete_onboarding(
                mem,
                user=user,
                state=state,
                code=CODE,
                waba_id=WABA,
                phone_number_id=PN_FAKE,
                cred_db=cred,
            )
    assert ei.value.status_code == 400
    assert cred.meta_credentials.docs == []
    assert mem.users.docs[0].get("meta_connection_status") != "connected"
    assert json.loads(r.get(f"{SESSION_PREFIX}{state}"))["consumed"] is False


@pytest.mark.asyncio
async def test_duplicate_pnid_409_does_not_name_owner():
    mem = MemDB()
    cred = SyncDB()
    a = _user(meta_phone_number_id=PN_A, meta_connection_status="connected")
    b = _user()
    mem.users.docs.extend([a, b])
    r = FakeRedis()
    state = _seed_state(r, user_id=str(b["_id"]))
    graph = GraphRouter()
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        with pytest.raises(HTTPException) as ei:
            await complete_onboarding(
                mem,
                user=b,
                state=state,
                code=CODE,
                waba_id=WABA,
                phone_number_id=PN_A,
                cred_db=cred,
            )
    assert ei.value.status_code == 409
    assert str(a["_id"]) not in str(ei.value.detail)
    assert a["email"] not in str(ei.value.detail)


@pytest.mark.asyncio
async def test_subscribe_failure_not_healthy_connected():
    mem = MemDB()
    cred = SyncDB()
    user = _user()
    mem.users.docs.append(user)
    r = FakeRedis()
    state = _seed_state(r, user_id=str(user["_id"]))
    graph = GraphRouter(subscribe_ok=False)
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        result = await complete_onboarding(
            mem,
            user=user,
            state=state,
            code=CODE,
            waba_id=WABA,
            phone_number_id=PN_A,
            cred_db=cred,
        )
    assert result["ok"] is False
    assert result["status"] == "error"
    assert mem.users.docs[0]["meta_connection_status"] == "error"
    assert cred.meta_credentials.docs  # token stored, status honest


@pytest.mark.asyncio
async def test_reconnect_same_and_new_pnid():
    mem = MemDB()
    cred = SyncDB()
    user = _user(
        meta_phone_number_id=PN_A,
        meta_waba_id=WABA,
        meta_connection_status="connected",
    )
    mem.users.docs.append(user)
    with _cfg():
        upsert_encrypted_access_token(
            user_id=str(user["_id"]), access_token="old-token-value", phone_number_id=PN_A, db=cred
        )
    r = FakeRedis()
    state = _seed_state(r, user_id=str(user["_id"]))
    graph = GraphRouter()
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        result = await complete_onboarding(
            mem,
            user=user,
            state=state,
            code=CODE,
            waba_id=WABA,
            phone_number_id=PN_A,
            cred_db=cred,
        )
        payload = json.loads(decrypt_secret(cred.meta_credentials.docs[0]["ciphertext"]))
        assert payload["t"] == TOKEN
        assert "old-token-value" not in str(cred.meta_credentials.docs[0])
    assert result["reconnected"] is True
    assert result["audit_action"] == "settings.meta_reconnected"

    r2 = FakeRedis()
    state2 = _seed_state(r2, user_id=str(user["_id"]), state="state_" + "D" * 24)
    graph2 = GraphRouter(pnid=PN_B)
    fresh = mem.users.docs[0]
    with _cfg(), _redis_ctx(r2), patch("app.services.meta_onboarding.httpx.Client", graph2):
        result2 = await complete_onboarding(
            mem,
            user=fresh,
            state=state2,
            code=CODE,
            waba_id=WABA,
            phone_number_id=PN_B,
            cred_db=cred,
        )
        payload2 = json.loads(decrypt_secret(cred.meta_credentials.docs[0]["ciphertext"]))
        assert payload2["p"] == PN_B
    assert result2["ok"] is True
    assert mem.users.docs[0]["meta_phone_number_id"] == PN_B
    assert mem.users.docs[0]["meta_last_phone_number_id"] == PN_A


@pytest.mark.asyncio
async def test_other_tenant_pnid_cannot_be_stolen():
    mem = MemDB()
    cred = SyncDB()
    owner = _user(meta_phone_number_id=PN_B, meta_connection_status="connected")
    attacker = _user()
    mem.users.docs.extend([owner, attacker])
    r = FakeRedis()
    state = _seed_state(r, user_id=str(attacker["_id"]))
    graph = GraphRouter(pnid=PN_B)
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        with pytest.raises(HTTPException) as ei:
            await complete_onboarding(
                mem,
                user=attacker,
                state=state,
                code=CODE,
                waba_id=WABA,
                phone_number_id=PN_B,
                cred_db=cred,
            )
    assert ei.value.status_code == 409
    assert attacker["_id"] != owner["_id"]
    assert mem.users.docs[1].get("meta_phone_number_id") != PN_B


@pytest.mark.asyncio
async def test_duplicate_key_restores_previous_credential():
    mem = MemDB()
    cred = SyncDB()
    user = _user(meta_phone_number_id=PN_A, meta_connection_status="connected")
    mem.users.docs.append(user)
    mem.users.raise_dup = True
    with _cfg():
        upsert_encrypted_access_token(
            user_id=str(user["_id"]), access_token="keep-old-token", phone_number_id=PN_A, db=cred
        )
    r = FakeRedis()
    state = _seed_state(r, user_id=str(user["_id"]))
    graph = GraphRouter(pnid=PN_B)
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        with pytest.raises(HTTPException) as ei:
            await complete_onboarding(
                mem,
                user=user,
                state=state,
                code=CODE,
                waba_id=WABA,
                phone_number_id=PN_B,
                cred_db=cred,
            )
        payload = json.loads(decrypt_secret(cred.meta_credentials.docs[0]["ciphertext"]))
        assert payload["t"] == "keep-old-token"
        assert payload["p"] == PN_A
    assert ei.value.status_code == 409


@pytest.mark.asyncio
async def test_disconnect_deletes_credential_blocks_send_keeps_history():
    mem = MemDB()
    cred = SyncDB()
    user = _user(
        meta_phone_number_id=PN_A,
        meta_waba_id=WABA,
        meta_connection_status="connected",
        meta_onboarding_source="embedded_signup",
    )
    mem.users.docs.append(user)
    mem.messages.docs.append(
        {"_id": ObjectId(), "user_id": str(user["_id"]), "provider": "meta", "message": "hello", "status": "delivered"}
    )
    with _cfg():
        upsert_encrypted_access_token(
            user_id=str(user["_id"]), access_token=TOKEN, phone_number_id=PN_A, db=cred
        )
        await disconnect_meta(mem, user=user, cred_db=cred)
        updated = mem.users.docs[0]
        assert updated["meta_connection_status"] == "disconnected"
        assert updated["meta_phone_number_id"] is None
        assert updated["meta_last_phone_number_id"] == PN_A
        assert updated["twilio_whatsapp_to"] == "+447700900001"
        assert cred.meta_credentials.docs == []
        assert len(mem.messages.docs) == 1
        assert mem.messages.docs[0]["message"] == "hello"
        with pytest.raises(MetaCredentialsError):
            get_meta_credentials_for_user(updated, db=cred)


@pytest.mark.asyncio
async def test_historical_status_after_disconnect():
    mem = MemDB()
    uid = ObjectId()
    user = _user(
        _id=uid,
        meta_phone_number_id=None,
        meta_last_phone_number_id=PN_A,
        meta_connection_status="disconnected",
    )
    mem.users.docs.append(user)
    wamid = "wamid.HIST1"
    mem.messages.docs.append(
        {
            "_id": ObjectId(),
            "user_id": str(uid),
            "direction": "outbound",
            "status": "sent",
            "provider": "meta",
            "provider_message_id": wamid,
        }
    )
    with patch("app.services.status_callback._publish_best_effort", MagicMock()):
        result = await apply_meta_status_update(
            provider_message_id=wamid,
            status_raw="delivered",
            phone_number_id=PN_A,
            db=mem,
        )
    assert result["updated"] is True
    assert mem.messages.docs[0]["status"] == "delivered"


def test_twilio_unaffected_and_runtime_source_of_truth():
    assert stored_provider({"provider": "twilio"}) == "twilio"
    assert stored_provider({"provider": "meta"}) == "meta"
    import app.services.inbound_whatsapp as inbound
    import app.services.whatsapp_outbound as outbound

    for mod in (inbound, outbound):
        src = inspect.getsource(mod)
        assert "WHATSAPP_PROVIDER" in src  # mentioned as never used, or
    src_out = inspect.getsource(outbound)
    assert "Never consults WHATSAPP_PROVIDER" in src_out


def test_settings_embedded_signup_available_and_no_token():
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_waba_id": WABA,
        "meta_connection_status": "connected",
        "meta_onboarding_source": "embedded_signup",
        "twilio_whatsapp_to": "+447700900001",
    }
    with _cfg(), patch("app.services.meta_credentials.tenant_meta_ready", return_value=True):
        out = build_whatsapp_settings(user)
    assert out["embedded_signup"]["available"] is True
    _no_secrets(out)
    assert out["meta"]["token_valid"] is True
    assert out["meta"]["phone_number_id"] == PN_A
    assert "poc_aligned" not in str(out["meta"].get("warnings"))
    with patch.multiple(
        "app.config.settings",
        META_APP_ID="",
        META_EMBEDDED_SIGNUP_CONFIG_ID="",
    ):
        out2 = build_whatsapp_settings(user)
    assert out2["embedded_signup"]["available"] is False


def test_legacy_poc_settings_wording():
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "legacy_poc",
    }
    with _cfg(), patch("app.services.meta_credentials.tenant_meta_ready", return_value=True):
        out = build_whatsapp_settings(user)
    assert out["meta"]["connection_status"] == "legacy_poc"
    assert any("legacy" in w.lower() for w in out["meta"]["warnings"])


def test_complete_http_no_token_in_body_or_audit(client):
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    r = FakeRedis()
    state = _seed_state(r, user_id=str(user["_id"]))
    graph = GraphRouter()
    cred = SyncDB()
    with (
        _cfg(),
        _redis_ctx(r),
        patch("app.services.meta_onboarding.httpx.Client", graph),
        patch("app.services.meta_onboarding.snapshot_credentials_row", return_value=None),
        patch("app.services.meta_onboarding.upsert_encrypted_access_token"),
        patch("app.services.meta_credentials.tenant_meta_ready", return_value=True),
        patch("app.middleware.auth.get_db", return_value=mem),
        patch("app.routes.settings.get_db", return_value=mem),
        patch("app.routes.settings.audit") as aud,
    ):
        # complete_onboarding uses cred_db=None → patch upsert above. Graph still runs.
        res = client.post(
            "/api/settings/whatsapp/meta/onboarding/complete",
            headers=_auth(user),
            json={
                "state": state,
                "code": CODE,
                "waba_id": WABA,
                "phone_number_id": PN_A,
            },
        )
    assert res.status_code == 200
    body = res.json()
    _no_secrets(body)
    assert body["onboarding"]["ok"] is True
    extra = str((aud.call_args.kwargs or {}).get("extra") or {})
    _no_secrets(extra)
    assert CODE.lower() not in extra.lower()
    assert aud.call_args.args[0] == "settings.meta_connected"
    _ = cred  # credentials go through 2I-A upsert mock


def test_no_graph_token_in_websocket():
    from app.routes import ws
    from app.services import ws_manager

    src = inspect.getsource(ws) + inspect.getsource(ws_manager)
    assert "META_ACCESS_TOKEN" not in src
    assert "EAA" not in src
    assert "graph.facebook.com" not in src


def test_phase2ia_encrypt_path_still_used():
    src = inspect.getsource(complete_onboarding)
    assert "upsert_encrypted_access_token" in src
    assert "encrypt_secret" not in src  # no second encryption path
    assert "AESGCM" not in src
