"""Phase 2I-C Meta production hardening (no live Graph/Twilio)."""
from __future__ import annotations

import inspect
from contextlib import asynccontextmanager
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.middleware.auth import create_access_token, hash_password
from app.models.common import utcnow
from app.services.meta_credentials import (
    MetaCredentialsError,
    delete_credentials_for_user,
    get_meta_credentials_for_user,
    is_meta_auth_death,
    maybe_mark_meta_auth_death,
    tenant_meta_ready,
    upsert_encrypted_access_token,
)
from app.services.meta_onboarding import (
    complete_onboarding,
    register_phone_number,
    start_onboarding_session,
    subscribe_waba,
)
from app.services.status_callback import apply_meta_status_update
from app.services.whatsapp_outbound import send_whatsapp_text
from app.services.whatsapp_settings import build_whatsapp_settings
from tests.test_meta_credentials_phase2ia import (
    KEY,
    PN_A,
    PN_B,
    TOKEN_A,
    WABA_A,
    SyncDB,
    _key_ctx,
)
from tests.test_meta_onboarding_phase2ib import (
    APP_ID,
    APP_SECRET,
    CODE,
    CONFIG_ID,
    TOKEN,
    WABA,
    FakeRedis,
    GraphRouter,
    MemDB,
    _cfg,
    _redis_ctx,
    _seed_state,
    _user,
)


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def _auth(user: dict) -> dict:
    return {"Authorization": f"Bearer {create_access_token(str(user['_id']), 'user')}"}


def _prod_settings(**extra) -> Settings:
    kw = dict(
        APP_ENV="production",
        META_ALLOW_LEGACY_POC_TOKEN=False,
        META_TOKEN_ENCRYPTION_KEY=KEY,
        JWT_SECRET="x" * 32,
        MONGO_URI="mongodb://localhost:27017",
        MONGO_DB="t",
        REDIS_URL="rediss://localhost:6379/0",
        TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        TWILIO_AUTH_TOKEN="tok",
        TWILIO_WHATSAPP_FROM="whatsapp:+14155238886",
        PUBLIC_BASE_URL="https://api.example.com",
        CORS_ORIGINS="https://app.example.com",
        META_WEBHOOK_VALIDATE_SIGNATURE=True,
        META_APP_ID=APP_ID,
        META_APP_SECRET=APP_SECRET,
        META_WEBHOOK_VERIFY_TOKEN="verify-token-not-real",
        AI_FEATURES_ENABLED=False,
        METRICS_ENABLED=False,
        RUN_INLINE_SCHEDULER=False,
    )
    kw.update(extra)
    return Settings(**kw)


def test_no_meta_configured_property():
    assert not hasattr(Settings, "meta_configured")
    import app.config as cfg

    assert "def meta_configured" not in inspect.getsource(cfg)


def test_production_env_token_cannot_authorize_send():
    db = SyncDB()
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_waba_id": WABA_A,
        "meta_connection_status": "connected",
    }
    with (
        _key_ctx(META_ACCESS_TOKEN=TOKEN_A, META_PHONE_NUMBER_ID=PN_A, META_WABA_ID=WABA_A),
        patch.object(
            __import__("app.services.meta_credentials", fromlist=["settings"]).settings,
            "APP_ENV",
            "production",
        ),
        patch("app.services.meta_credentials.settings.META_ALLOW_LEGACY_POC_TOKEN", False),
    ):
        with pytest.raises(MetaCredentialsError):
            get_meta_credentials_for_user(user, db=db)
        from app.services import meta_whatsapp_service

        with pytest.raises(Exception):
            meta_whatsapp_service.send_text(to="+447700900123", text="hi", user=user)


def test_production_env_pnid_not_routing_authority():
    import app.services.inbound_whatsapp as inbound

    src = inspect.getsource(inbound)
    assert "META_PHONE_NUMBER_ID" not in src
    assert "meta_last_phone_number_id" not in src
    assert "meta_phone_number_id" in src


def test_production_env_waba_not_template_sync_authority():
    from app.services import meta_templates as mt

    src = inspect.getsource(mt.fetch_graph_message_templates)
    assert "META_WABA_ID" not in src
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_waba_id": "",
        "meta_connection_status": "connected",
    }
    db = SyncDB()
    with _key_ctx(META_WABA_ID=WABA_A, META_ACCESS_TOKEN=TOKEN_A):
        upsert_encrypted_access_token(
            user_id=str(user["_id"]), access_token=TOKEN_A, phone_number_id=PN_A, db=db
        )
        creds = get_meta_credentials_for_user(user, db=db)
        assert creds.waba_id == ""
        assert creds.waba_id != WABA_A


def test_legacy_flag_rejected_at_production_startup():
    s = _prod_settings(META_ALLOW_LEGACY_POC_TOKEN=True)
    with pytest.raises(RuntimeError, match="META_ALLOW_LEGACY_POC_TOKEN"):
        s.validate_for_startup()


def test_legacy_fallback_dev_only():
    db = SyncDB()
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "legacy_poc",
    }
    with (
        _key_ctx(META_ALLOW_LEGACY_POC_TOKEN=True, META_ACCESS_TOKEN=TOKEN_A, META_PHONE_NUMBER_ID=PN_A),
        patch.object(
            __import__("app.services.meta_credentials", fromlist=["settings"]).settings,
            "APP_ENV",
            "dev",
        ),
    ):
        creds = get_meta_credentials_for_user(user, db=db)
        assert creds.access_token == TOKEN_A
    with (
        _key_ctx(META_ALLOW_LEGACY_POC_TOKEN=True, META_ACCESS_TOKEN=TOKEN_A, META_PHONE_NUMBER_ID=PN_A),
        patch.object(
            __import__("app.services.meta_credentials", fromlist=["settings"]).settings,
            "APP_ENV",
            "production",
        ),
    ):
        with pytest.raises(MetaCredentialsError, match="not allowed"):
            get_meta_credentials_for_user(user, db=db)


def test_malformed_encryption_key_fails_startup():
    s = _prod_settings(META_TOKEN_ENCRYPTION_KEY="base64:%%%%not-valid%%%%")
    with pytest.raises(RuntimeError, match="META_TOKEN_ENCRYPTION_KEY"):
        s.validate_for_startup()
    s2 = _prod_settings(META_TOKEN_ENCRYPTION_KEY="short")
    with pytest.raises(RuntimeError, match="META_TOKEN_ENCRYPTION_KEY"):
        s2.validate_for_startup()
    s3 = _prod_settings(META_TOKEN_ENCRYPTION_KEY="x" * 32)
    with pytest.raises(RuntimeError, match="must not equal JWT_SECRET"):
        s3.validate_for_startup()


def test_startup_requires_app_secrets_not_legacy_env():
    s = _prod_settings(META_APP_ID="", META_ACCESS_TOKEN="env-tok", META_PHONE_NUMBER_ID=PN_A)
    with pytest.raises(RuntimeError, match="META_APP_ID"):
        s.validate_for_startup()
    src = inspect.getsource(Settings.validate_for_startup)
    assert "META_ACCESS_TOKEN is required" not in src
    assert "META_PHONE_NUMBER_ID is required" not in src
    assert "META_WABA_ID is required" not in src
    import worker
    import scheduler

    assert "validate_for_startup" in inspect.getsource(worker)
    assert "validate_for_startup" in inspect.getsource(scheduler)


def test_expired_credential_rejected_and_sender_not_ready():
    from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility

    db = SyncDB()
    uid = ObjectId()
    user = {
        "_id": uid,
        "meta_phone_number_id": PN_A,
        "meta_waba_id": WABA_A,
        "meta_connection_status": "connected",
    }
    with _key_ctx():
        upsert_encrypted_access_token(
            user_id=str(uid),
            access_token=TOKEN_A,
            phone_number_id=PN_A,
            expires_at=utcnow() - timedelta(seconds=5),
            db=db,
        )
        with pytest.raises(MetaCredentialsError, match="expired"):
            get_meta_credentials_for_user(user, db=db)
        assert tenant_meta_ready(user, db=db) is False
        with patch("app.services.meta_credentials._coll", return_value=db.meta_credentials):
            out = build_whatsapp_settings(user)
            assert out["meta"]["token_valid"] is False
            assert out["meta"]["status"] == "requires_action"
            lead = {
                "phone": "+447700900123",
                "whatsapp_consent_status": "opted_in",
                "blacklisted": False,
            }
            elig = get_whatsapp_send_eligibility(
                lead=lead, purpose="conversational", has_template=True, provider="meta", user=user
            )
            assert elig.sender_configured is False


def test_auth_death_classification_and_mark():
    assert is_meta_auth_death(http_status=401, error=None) is True
    assert is_meta_auth_death(http_status=400, error={"code": 190}) is True
    assert is_meta_auth_death(http_status=400, error={"code": 102}) is True
    assert is_meta_auth_death(http_status=400, error={"code": 463}) is True
    assert is_meta_auth_death(http_status=400, error={"code": 467}) is True
    assert is_meta_auth_death(http_status=400, error={"code": 100}) is False
    assert is_meta_auth_death(http_status=429, error={"code": 190}) is False
    assert is_meta_auth_death(http_status=500, error={"code": 190}) is False
    assert is_meta_auth_death(http_status=400, error={"code": 131047}) is False
    db = SyncDB()
    uid = ObjectId()
    user = {
        "_id": uid,
        "meta_phone_number_id": PN_A,
        "meta_waba_id": WABA_A,
        "meta_connection_status": "connected",
    }
    db.users.docs.append(dict(user))
    maybe_mark_meta_auth_death(user, http_status=401, error={"code": 190}, db=db)
    assert db.users.docs[0]["meta_connection_status"] == "error"
    assert db.users.docs[0]["meta_phone_number_id"] == PN_A
    maybe_mark_meta_auth_death(user, http_status=400, error={"code": 100}, db=db)
    assert db.users.docs[0]["meta_phone_number_id"] == PN_A


def test_graph_auth_error_marks_connection_error_send_path():
    from app.services import meta_whatsapp_service

    db = SyncDB()
    uid = ObjectId()
    user = {
        "_id": uid,
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "connected",
        "meta_waba_id": WABA_A,
    }
    db.users.docs.append(dict(user))

    class _FakeResp:
        is_error = True
        status_code = 401

        def json(self):
            return {"error": {"code": 190, "message": "Invalid OAuth access token"}}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _FakeResp()

    with _key_ctx():
        upsert_encrypted_access_token(
            user_id=str(uid), access_token=TOKEN_A, phone_number_id=PN_A, db=db
        )
        with (
            patch("app.services.meta_credentials._coll", return_value=db.meta_credentials),
            patch("app.services.meta_whatsapp_service.httpx.Client", _FakeClient),
            patch("app.services.meta_credentials._sync_db", return_value=db),
        ):
            with pytest.raises(meta_whatsapp_service.MetaWhatsAppError):
                meta_whatsapp_service.send_text(to="+447700900123", text="hi", user=user)
    assert db.users.docs[0]["meta_connection_status"] == "error"


def test_normal_400_and_429_do_not_mark_token_error():
    from app.services import meta_whatsapp_service

    db = SyncDB()
    uid = ObjectId()
    user = {
        "_id": uid,
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "connected",
        "meta_waba_id": WABA_A,
    }
    db.users.docs.append(dict(user))

    def _run(status, payload):
        class _FakeResp:
            is_error = True
            status_code = status

            def json(self):
                return payload

        class _FakeClient:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _FakeResp()

        with _key_ctx():
            upsert_encrypted_access_token(
                user_id=str(uid), access_token=TOKEN_A, phone_number_id=PN_A, db=db
            )
            with (
                patch("app.services.meta_credentials._coll", return_value=db.meta_credentials),
                patch("app.services.meta_whatsapp_service.httpx.Client", _FakeClient),
                patch("app.services.meta_credentials._sync_db", return_value=db),
            ):
                with pytest.raises(meta_whatsapp_service.MetaWhatsAppError):
                    meta_whatsapp_service.send_text(to="+447700900123", text="hi", user=user)

    db.users.docs[0]["meta_connection_status"] = "connected"
    _run(400, {"error": {"code": 100, "message": "bad input"}})
    assert db.users.docs[0]["meta_connection_status"] == "connected"
    _run(429, {"error": {"code": 4, "message": "rate limit"}})
    assert db.users.docs[0]["meta_connection_status"] == "connected"


def test_last_pnid_not_used_for_send_or_inbound():
    from app.services import meta_whatsapp_service
    import app.services.inbound_whatsapp as inbound
    import app.services.whatsapp_eligibility as elig
    import app.services.meta_media as media
    import app.services.meta_templates as templates

    for mod in (meta_whatsapp_service, inbound, elig, media, templates):
        assert "meta_last_phone_number_id" not in inspect.getsource(mod)
    src_idx = inspect.getsource(__import__("app.db.mongo", fromlist=["init_indexes"]).init_indexes)
    assert "meta_last_phone_number_id" not in src_idx


@pytest.mark.asyncio
async def test_last_pnid_status_callback_still_works():
    mem = MemDB()
    uid = ObjectId()
    user = _user(
        _id=uid,
        meta_phone_number_id=PN_B,
        meta_last_phone_number_id=PN_A,
        meta_connection_status="connected",
    )
    mem.users.docs.append(user)
    wamid = "wamid.LAST1"
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


@pytest.mark.asyncio
async def test_reconnect_new_pnid_stores_previous_as_last():
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
    graph = GraphRouter(pnid=PN_B)
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        result = await complete_onboarding(
            mem,
            user=user,
            state=state,
            code=CODE,
            waba_id=WABA,
            phone_number_id=PN_B,
            cred_db=cred,
        )
    assert result["ok"] is True
    assert mem.users.docs[0]["meta_phone_number_id"] == PN_B
    assert mem.users.docs[0]["meta_last_phone_number_id"] == PN_A


def test_delete_user_removes_meta_credentials():
    from app.routes import admin as admin_mod

    assert "delete_credentials_for_user" in inspect.getsource(admin_mod.delete_user)
    db = SyncDB()
    uid = ObjectId()
    with _key_ctx():
        upsert_encrypted_access_token(
            user_id=str(uid), access_token=TOKEN_A, phone_number_id=PN_A, db=db
        )
        assert db.meta_credentials.docs
        delete_credentials_for_user(user_id=str(uid), db=db)
        assert db.meta_credentials.docs == []


def test_poc_test_send_unavailable_production(client):
    user = {
        "_id": ObjectId(),
        "email": "u@example.com",
        "password_hash": hash_password("Password1"),
        "role": "user",
        "plan": "free",
        "banned": False,
        "active": True,
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "connected",
    }
    mem = MemDB()
    mem.users.docs.append(user)
    with (
        patch.object(
            __import__("app.routes.meta_poc", fromlist=["settings"]).settings,
            "APP_ENV",
            "production",
        ),
        patch("app.middleware.auth.get_db", return_value=mem),
    ):
        res = client.post(
            "/api/meta/test-send",
            headers=_auth(user),
            json={"to": "+447700900123", "message": "hi"},
        )
    assert res.status_code == 404


def test_poc_test_send_dev_uses_tenant_resolver():
    from app.routes import meta_poc
    from app.services import meta_whatsapp_service

    src = inspect.getsource(meta_poc.meta_test_send)
    assert "user=user" in src
    assert "META_ACCESS_TOKEN" not in src
    assert "is_production_like" in src
    user = {"_id": ObjectId(), "meta_phone_number_id": PN_A, "meta_connection_status": "connected"}
    with patch("app.services.meta_credentials.get_meta_credentials_for_user") as g:
        g.side_effect = MetaCredentialsError("Meta credentials are not configured for this account")
        with pytest.raises(Exception):
            meta_whatsapp_service.send_text(to="+447700900123", text="hi", user=user)
        g.assert_called()


def test_embedded_signup_available_requires_app_secret():
    from app.config import settings as cfg

    with patch.multiple(
        cfg,
        META_APP_ID=APP_ID,
        META_APP_SECRET=APP_SECRET,
        META_EMBEDDED_SIGNUP_CONFIG_ID=CONFIG_ID,
    ):
        assert cfg.embedded_signup_available is True
    with patch.multiple(
        cfg,
        META_APP_ID=APP_ID,
        META_APP_SECRET="",
        META_EMBEDDED_SIGNUP_CONFIG_ID=CONFIG_ID,
    ):
        assert cfg.embedded_signup_available is False
        user = _user(meta_phone_number_id=PN_A, meta_connection_status="connected")
        with patch("app.services.meta_credentials.tenant_meta_ready", return_value=True):
            out = build_whatsapp_settings(user)
        assert out["embedded_signup"]["available"] is False
        assert APP_SECRET not in str(out)


def test_strict_subscribed_apps_and_register_success():
    with patch(
        "app.services.meta_onboarding._graph_post",
        return_value={"ok": True, "status_code": 200, "data": {"success": True}},
    ):
        assert subscribe_waba(access_token="t", waba_id=WABA) is True
    with patch(
        "app.services.meta_onboarding._graph_post",
        return_value={"ok": True, "status_code": 200, "data": {}},
    ):
        assert subscribe_waba(access_token="t", waba_id=WABA) is False
    with patch(
        "app.services.meta_onboarding._graph_post",
        return_value={"ok": True, "status_code": 200, "data": {"success": True}},
    ):
        ok, warn = register_phone_number(access_token="t", phone_number_id=PN_A)
        assert ok is True
        assert warn is None
    with patch(
        "app.services.meta_onboarding._graph_post",
        return_value={"ok": True, "status_code": 200, "data": {}},
    ):
        ok, warn = register_phone_number(access_token="t", phone_number_id=PN_A)
        assert ok is False
    with patch(
        "app.services.meta_onboarding._graph_post",
        return_value={
            "ok": False,
            "status_code": 400,
            "data": {"error": {"code": 133010, "message": "already registered"}},
        },
    ):
        ok, warn = register_phone_number(access_token="t", phone_number_id=PN_A)
        assert ok is True
    with patch(
        "app.services.meta_onboarding._graph_post",
        return_value={
            "ok": False,
            "status_code": 400,
            "data": {"error": {"code": 133016, "message": "already registered"}},
        },
    ):
        ok, warn = register_phone_number(access_token="t", phone_number_id=PN_A)
        assert ok is True
    with patch(
        "app.services.meta_onboarding._graph_post",
        return_value={
            "ok": False,
            "status_code": 400,
            "data": {"error": {"code": 100, "message": "pin required"}},
        },
    ):
        ok, warn = register_phone_number(access_token="t", phone_number_id=PN_A)
        assert ok is False


@pytest.mark.asyncio
async def test_onboarding_user_update_failure_restores_snapshot():
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
            user_id=str(user["_id"]), access_token="keep-old-token", phone_number_id=PN_A, db=cred
        )
    r = FakeRedis()
    state = _seed_state(r, user_id=str(user["_id"]))
    graph = GraphRouter()

    async def boom(*a, **k):
        raise RuntimeError("user write failed")

    mem.users.update_one = boom
    with _cfg(), _redis_ctx(r), patch("app.services.meta_onboarding.httpx.Client", graph):
        with pytest.raises(HTTPException) as ei:
            await complete_onboarding(
                mem,
                user=user,
                state=state,
                code=CODE,
                waba_id=WABA,
                phone_number_id=PN_A,
                cred_db=cred,
            )
        assert ei.value.status_code == 502
        payload = json_loads_safe(cred)
        assert payload["t"] == "keep-old-token"


def json_loads_safe(cred: SyncDB) -> dict:
    import json
    from app.services.meta_credentials import decrypt_secret

    return json.loads(decrypt_secret(cred.meta_credentials.docs[0]["ciphertext"]))


def test_redis_onboarding_fails_closed():
    with (
        _cfg(),
        patch("app.services.meta_onboarding._redis", side_effect=ConnectionError("redis down")),
        patch("app.services.meta_onboarding.exchange_authorization_code") as ex,
    ):
        with pytest.raises(ConnectionError):
            start_onboarding_session(user_id=str(ObjectId()))
        ex.assert_not_called()


@pytest.mark.asyncio
async def test_redis_complete_fails_closed_no_exchange():
    mem = MemDB()
    user = _user()
    mem.users.docs.append(user)
    with (
        _cfg(),
        patch("app.services.meta_onboarding._redis", side_effect=ConnectionError("redis down")),
        patch("app.services.meta_onboarding.exchange_authorization_code") as ex,
    ):
        with pytest.raises(ConnectionError):
            await complete_onboarding(
                mem,
                user=user,
                state="state_" + "Z" * 24,
                code=CODE,
                waba_id=WABA,
                phone_number_id=PN_A,
            )
        ex.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_from_error_to_connected():
    mem = MemDB()
    cred = SyncDB()
    user = _user(
        meta_phone_number_id=PN_A,
        meta_waba_id=WABA,
        meta_connection_status="error",
    )
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
    assert result["ok"] is True
    assert result["status"] == "connected"
    assert result["reconnected"] is True
    assert mem.users.docs[0]["meta_connection_status"] == "connected"


def test_twilio_send_and_campaigns_unaffected():
    with patch(
        "app.services.whatsapp_outbound.twilio_service.send_whatsapp",
        return_value={"sid": "SM1", "status": "queued"},
    ) as tw:
        out = send_whatsapp_text(provider="twilio", to="+447700900123", text="hi")
    tw.assert_called_once()
    assert out["provider"] == "twilio"
    import app.services.campaign_provider as cp
    import app.workers.campaign_tasks as ct
    import app.workers.tasks as wt

    for mod in (cp, ct, wt):
        src = inspect.getsource(mod)
        assert "twilio" in src.lower()


def test_migration_script_production_guard():
    import scripts.migrate_meta_poc_credentials as mig

    src = inspect.getsource(mig)
    assert "--allow-production-poc-tenant" in src
    assert "migrate_legacy_poc_user" in src
    assert "print(token)" not in src
    import asyncio

    with patch.object(mig.settings, "APP_ENV", "production"):
        rc = asyncio.run(mig._run(dry_run=True, allow_production_poc_tenant=False))
    assert rc == 2


def test_onboarding_lock_ttl_documented():
    import app.services.meta_onboarding as mo

    src = inspect.getsource(mo._acquire_inflight)
    assert "ex=90" in src
    assert "ownership token" in src.lower() or "Do not DEL on success" in src
