"""Phase 2I-A tenant Meta credentials (no live Graph/Twilio)."""
from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

from app.config import Settings
from app.services.meta_credentials import (
    MetaCredentialsError,
    MetaTenantCredentials,
    decrypt_secret,
    encrypt_secret,
    get_meta_credentials_for_user,
    load_encryption_key,
    migrate_legacy_poc_user,
    tenant_meta_ready,
    upsert_encrypted_access_token,
)
from app.services.whatsapp_settings import build_whatsapp_settings


KEY = "k" * 32
TOKEN_A = "tenant-a-token-secret"
TOKEN_B = "tenant-b-token-secret"
PN_A = "111111111111111"
PN_B = "222222222222222"
WABA_A = "555555555555555"


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
        self.users = SyncColl()


def _key_ctx(**extra):
    kw = {"META_TOKEN_ENCRYPTION_KEY": KEY, "META_ALLOW_LEGACY_POC_TOKEN": False, **extra}
    return patch.multiple("app.services.meta_credentials.settings", **kw)


def test_encrypt_decrypt_roundtrip_and_not_plaintext():
    with _key_ctx():
        ct = encrypt_secret("hello-secret")
        assert "hello-secret" not in ct
        assert decrypt_secret(ct) == "hello-secret"
        ct2 = encrypt_secret("hello-secret")
        assert ct != ct2


def test_wrong_key_and_tamper_fail():
    with _key_ctx():
        ct = encrypt_secret("hello-secret")
    other = load_encryption_key("z" * 32)
    with pytest.raises(MetaCredentialsError):
        decrypt_secret(ct, key=other)
    tampered = ct[:-4] + ("AAAA" if not ct.endswith("AAAA") else "BBBB")
    with _key_ctx(), pytest.raises(MetaCredentialsError):
        decrypt_secret(tampered)


def test_token_encrypted_at_rest_not_in_mongo():
    db = SyncDB()
    uid = str(ObjectId())
    with _key_ctx():
        upsert_encrypted_access_token(
            user_id=uid, access_token=TOKEN_A, phone_number_id=PN_A, db=db
        )
    row = db.meta_credentials.docs[0]
    blob = str(row)
    assert TOKEN_A not in blob
    assert "ciphertext" in row
    assert row["algorithm"] == "AESGCM"


def test_resolver_current_user_and_isolation():
    db = SyncDB()
    a = ObjectId()
    b = ObjectId()
    with _key_ctx():
        upsert_encrypted_access_token(user_id=str(a), access_token=TOKEN_A, phone_number_id=PN_A, db=db)
        upsert_encrypted_access_token(user_id=str(b), access_token=TOKEN_B, phone_number_id=PN_B, db=db)
        user_a = {
            "_id": a,
            "meta_phone_number_id": PN_A,
            "meta_waba_id": WABA_A,
            "meta_connection_status": "connected",
        }
        creds = get_meta_credentials_for_user(user_a, db=db)
        assert creds.access_token == TOKEN_A
        assert creds.phone_number_id == PN_A
        user_a_steal = dict(user_a)
        user_a_steal["_id"] = b
        with pytest.raises(MetaCredentialsError):
            get_meta_credentials_for_user(user_a_steal, db=db)


def test_pnid_mismatch_fails():
    db = SyncDB()
    uid = ObjectId()
    with _key_ctx():
        upsert_encrypted_access_token(user_id=str(uid), access_token=TOKEN_A, phone_number_id=PN_A, db=db)
        user = {
            "_id": uid,
            "meta_phone_number_id": PN_B,
            "meta_waba_id": WABA_A,
            "meta_connection_status": "connected",
        }
        with pytest.raises(MetaCredentialsError, match="does not match"):
            get_meta_credentials_for_user(user, db=db)


def test_production_missing_and_no_env_fallback():
    db = SyncDB()
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_waba_id": WABA_A,
        "meta_connection_status": "connected",
    }
    with (
        _key_ctx(META_ACCESS_TOKEN="env-token-must-not-use", META_PHONE_NUMBER_ID=PN_A),
        patch.object(__import__("app.services.meta_credentials", fromlist=["settings"]).settings, "APP_ENV", "production"),
    ):
        with pytest.raises(MetaCredentialsError):
            get_meta_credentials_for_user(user, db=db)


def test_production_legacy_flag_rejected():
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "legacy_poc",
    }
    with (
        _key_ctx(META_ALLOW_LEGACY_POC_TOKEN=True, META_ACCESS_TOKEN="tok", META_PHONE_NUMBER_ID=PN_A),
        patch.object(__import__("app.services.meta_credentials", fromlist=["settings"]).settings, "APP_ENV", "production"),
    ):
        with pytest.raises(MetaCredentialsError, match="not allowed"):
            get_meta_credentials_for_user(user)


def test_dev_legacy_fallback_rules():
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_waba_id": WABA_A,
        "meta_connection_status": "legacy_poc",
    }
    with _key_ctx(
        META_ALLOW_LEGACY_POC_TOKEN=True,
        META_ACCESS_TOKEN="env-tok",
        META_PHONE_NUMBER_ID=PN_A,
        META_WABA_ID=WABA_A,
        APP_ENV="dev",
    ):
        creds = get_meta_credentials_for_user(user)
        assert creds.access_token == "env-tok"

    with _key_ctx(
        META_ALLOW_LEGACY_POC_TOKEN=False,
        META_ACCESS_TOKEN="env-tok",
        META_PHONE_NUMBER_ID=PN_A,
        APP_ENV="dev",
    ):
        with pytest.raises(MetaCredentialsError):
            get_meta_credentials_for_user(user)

    user["meta_connection_status"] = "connected"
    with _key_ctx(
        META_ALLOW_LEGACY_POC_TOKEN=True,
        META_ACCESS_TOKEN="env-tok",
        META_PHONE_NUMBER_ID=PN_A,
        APP_ENV="dev",
    ):
        with pytest.raises(MetaCredentialsError):
            get_meta_credentials_for_user(user)

    user["meta_connection_status"] = "legacy_poc"
    user["meta_phone_number_id"] = PN_B
    with _key_ctx(
        META_ALLOW_LEGACY_POC_TOKEN=True,
        META_ACCESS_TOKEN="env-tok",
        META_PHONE_NUMBER_ID=PN_A,
        APP_ENV="dev",
    ):
        with pytest.raises(MetaCredentialsError):
            get_meta_credentials_for_user(user)


def test_settings_response_has_no_token():
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_waba_id": WABA_A,
        "meta_connection_status": "connected",
        "twilio_whatsapp_to": "+447700900001",
    }
    db = SyncDB()
    with _key_ctx():
        upsert_encrypted_access_token(user_id=str(user["_id"]), access_token=TOKEN_A, phone_number_id=PN_A, db=db)
        with patch("app.services.meta_credentials.tenant_meta_ready", return_value=True):
            out = build_whatsapp_settings(user)
    blob = str(out).lower()
    assert TOKEN_A.lower() not in blob
    assert "access_token" not in blob
    assert out["meta"]["webhook_ready"] is None
    assert out["embedded_signup"]["available"] is False
    assert "token_valid" in out["meta"]


@pytest.mark.asyncio
async def test_inbound_disconnected_ignored():
    from app.services.inbound_whatsapp import InboundMessage, _resolve_tenant

    class UColl:
        def __init__(self):
            self.docs = [
                {
                    "_id": ObjectId(),
                    "meta_phone_number_id": PN_A,
                    "meta_connection_status": "disconnected",
                }
            ]

        async def find_one(self, q=None, **k):
            q = q or {}
            for d in self.docs:
                if d.get("meta_phone_number_id") == q.get("meta_phone_number_id"):
                    return copy.deepcopy(d)
            return None

    class DB:
        users = UColl()

    inbound = InboundMessage(
        provider="meta",
        provider_message_id="wamid.x",
        customer_phone="+447700900123",
        business_identifier=PN_A,
        body="hi",
        profile_name=None,
        timestamp=None,
        message_type="text",
    )
    assert await _resolve_tenant(DB(), inbound) is None


@pytest.mark.asyncio
async def test_inbound_connected_works():
    from app.services.inbound_whatsapp import InboundMessage, _resolve_tenant

    uid = ObjectId()

    class UColl:
        async def find_one(self, q=None, **k):
            return {
                "_id": uid,
                "meta_phone_number_id": PN_A,
                "meta_connection_status": "connected",
            }

    class DB:
        users = UColl()

    inbound = InboundMessage(
        provider="meta",
        provider_message_id="wamid.x",
        customer_phone="+447700900123",
        business_identifier=PN_A,
        body="hi",
        profile_name=None,
        timestamp=None,
        message_type="text",
    )
    got = await _resolve_tenant(DB(), inbound)
    assert got["_id"] == uid


@pytest.mark.asyncio
async def test_status_callback_no_env_pnid_required():
    from app.services.status_callback import apply_meta_status_update
    from tests.test_meta_inbound_phase2a import MemDB

    uid = ObjectId()
    mem = MemDB()
    mem.users.docs.append(
        {"_id": uid, "meta_phone_number_id": PN_A, "meta_connection_status": "disconnected"}
    )
    mem.messages.docs.append(
        {
            "_id": ObjectId(),
            "user_id": str(uid),
            "lead_id": str(ObjectId()),
            "direction": "outbound",
            "message": "hi",
            "status": "sent",
            "provider": "meta",
            "provider_message_id": "wamid.Z",
        }
    )
    with (
        patch("app.config.settings.META_PHONE_NUMBER_ID", PN_B),
        patch("app.services.status_callback._publish_best_effort"),
    ):
        ok = await apply_meta_status_update(
            provider_message_id="wamid.Z",
            status_raw="delivered",
            errors=[],
            phone_number_id=PN_A,
            db=mem,
        )
        bad = await apply_meta_status_update(
            provider_message_id="wamid.Z",
            status_raw="read",
            errors=[],
            phone_number_id=PN_B,
            db=mem,
        )
    assert ok.get("updated") is True
    assert bad.get("reason") == "pnid_mismatch"


def test_eligibility_uses_tenant_connection():
    from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility

    lead = {"phone": "+447700900123", "whatsapp_consent_status": "opted_in", "blacklisted": False}
    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "connected",
        "meta_waba_id": WABA_A,
    }
    with patch("app.services.meta_credentials.tenant_meta_ready", return_value=True):
        elig = get_whatsapp_send_eligibility(
            lead=lead, purpose="conversational", has_template=True, provider="meta", user=user
        )
    assert elig.sender_configured is True
    with patch("app.services.meta_credentials.tenant_meta_ready", return_value=False):
        elig2 = get_whatsapp_send_eligibility(
            lead=lead, purpose="conversational", has_template=True, provider="meta", user=user
        )
    assert elig2.sender_configured is False


def test_startup_rejects_legacy_flag_in_production():
    s = Settings(
        APP_ENV="production",
        META_ALLOW_LEGACY_POC_TOKEN=True,
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
        META_APP_ID="111222333444555",
        META_APP_SECRET="test-app-secret-not-real",
        META_WEBHOOK_VERIFY_TOKEN="verify-token-not-real",
        AI_FEATURES_ENABLED=False,
        METRICS_ENABLED=False,
        RUN_INLINE_SCHEDULER=False,
    )
    with pytest.raises(RuntimeError, match="META_ALLOW_LEGACY_POC_TOKEN"):
        s.validate_for_startup()


def test_send_text_uses_tenant_token_not_env():
    from app.services import meta_whatsapp_service

    captured = {}

    class _FakeResp:
        is_error = False
        status_code = 200

        def json(self):
            return {"messages": [{"id": "wamid.1"}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["authorization"] = (headers or {}).get("Authorization")
            captured["url"] = url
            return _FakeResp()

    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "connected",
        "meta_waba_id": WABA_A,
    }
    creds = MetaTenantCredentials(access_token=TOKEN_A, phone_number_id=PN_A, waba_id=WABA_A)
    with (
        patch("app.services.meta_whatsapp_service.settings.META_ACCESS_TOKEN", "env-must-not"),
        patch("app.services.meta_whatsapp_service.settings.META_PHONE_NUMBER_ID", PN_B),
        patch("app.services.meta_whatsapp_service.settings.META_GRAPH_VERSION", "v21.0"),
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=creds),
        patch("app.services.meta_whatsapp_service.httpx.Client", _FakeClient),
    ):
        result = meta_whatsapp_service.send_text(to="+447700900123", text="hi", user=user)
    assert captured["authorization"] == f"Bearer {TOKEN_A}"
    assert PN_A in captured["url"]
    assert PN_B not in captured["url"]
    assert result.provider_message_id == "wamid.1"


def test_send_template_uses_tenant_token():
    from app.services.meta_whatsapp_service import send_template

    captured = {}

    class _FakeResp:
        is_error = False
        status_code = 200

        def json(self):
            return {"messages": [{"id": "wamid.T"}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["authorization"] = (headers or {}).get("Authorization")
            return _FakeResp()

    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "connected",
        "meta_waba_id": WABA_A,
    }
    creds = MetaTenantCredentials(access_token=TOKEN_A, phone_number_id=PN_A, waba_id=WABA_A)
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=creds),
        patch("app.services.meta_whatsapp_service.httpx.Client", _FakeClient),
        patch("app.services.meta_whatsapp_service.settings.META_GRAPH_VERSION", "v21.0"),
    ):
        send_template(to="+447700900123", name="t", language_code="en", user=user)
    assert captured["authorization"] == f"Bearer {TOKEN_A}"


def test_outbound_dispatcher_passes_user():
    from app.services.whatsapp_outbound import send_whatsapp_text, send_whatsapp_template

    user = {"_id": ObjectId(), "meta_phone_number_id": PN_A, "meta_connection_status": "connected"}
    creds = MetaTenantCredentials(access_token=TOKEN_A, phone_number_id=PN_A, waba_id=WABA_A)
    fake = MagicMock()
    fake.provider_message_id = "wamid.x"
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=creds),
        patch("app.services.whatsapp_outbound.meta_whatsapp_service.send_text", return_value=fake) as st,
        patch("app.services.whatsapp_outbound.meta_whatsapp_service.send_template", return_value=fake) as stm,
    ):
        send_whatsapp_text(provider="meta", to="+447700900123", text="hi", user=user)
        send_whatsapp_template(
            provider="meta", to="+447700900123", name="n", language_code="en", user=user
        )
    assert st.call_args.kwargs["user"] is user
    assert stm.call_args.kwargs["user"] is user


def test_twilio_outbound_unchanged():
    from app.services.whatsapp_outbound import send_whatsapp_text

    with patch(
        "app.services.whatsapp_outbound.twilio_service.send_whatsapp",
        return_value={"sid": "SM1", "status": "queued"},
    ) as tw:
        out = send_whatsapp_text(provider="twilio", to="+447700900123", text="hi")
    tw.assert_called_once()
    assert out["provider"] == "twilio"
    assert out["provider_message_id"] == "SM1"


def test_migrate_dry_run_does_not_write():
    db = SyncDB()
    uid = ObjectId()
    user = {"_id": uid, "meta_phone_number_id": PN_A}
    db.users.docs.append(user)
    with _key_ctx(META_ACCESS_TOKEN=TOKEN_A, META_PHONE_NUMBER_ID=PN_A, META_WABA_ID=WABA_A):
        out = migrate_legacy_poc_user(user, dry_run=True, db=db)
    assert out["eligible"] is True
    assert out["updated"] is False
    assert db.meta_credentials.docs == []


def test_poc_test_send_uses_resolver():
    import inspect

    from app.routes import meta_poc
    from app.services import meta_whatsapp_service

    user = {"_id": ObjectId(), "meta_phone_number_id": PN_A, "meta_connection_status": "connected"}
    with patch("app.services.meta_credentials.get_meta_credentials_for_user") as g:
        g.side_effect = MetaCredentialsError("Meta credentials are not configured for this account")
        with pytest.raises(Exception):
            meta_whatsapp_service.send_text(to="+447700900123", text="hi", user=user)
        g.assert_called()

    src = inspect.getsource(meta_poc.meta_test_send)
    assert "user=user" in src
    assert "META_ACCESS_TOKEN" not in src


@pytest.mark.asyncio
async def test_media_metadata_and_binary_use_tenant_token():
    import httpx

    from app.services.meta_media import download_and_store_meta_media

    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization") or "")
        if "/MEDIA99" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=MEDIA99",
                    "mime_type": "image/jpeg",
                    "file_size": 4,
                },
            )
        return httpx.Response(200, content=b"\xff\xd8\xff\xd9", headers={"content-type": "image/jpeg"})

    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "connected",
        "meta_waba_id": WABA_A,
    }
    creds = MetaTenantCredentials(access_token=TOKEN_A, phone_number_id=PN_A, waba_id=WABA_A)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=False) as client:
        with (
            patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=creds),
            patch("app.services.meta_media.get_media_storage") as gs,
            patch("app.services.meta_media.resolve_upload_mime", return_value="image/jpeg"),
            patch("app.services.meta_media.is_allowed_mime", return_value=True),
            patch("app.services.meta_media.assert_safe_meta_media_url", side_effect=lambda u: u),
        ):
            gs.return_value.save.return_value = {
                "id": "fid",
                "path": "/api/media/files/fid",
                "filename": "image.jpg",
            }
            await download_and_store_meta_media(
                media_id="MEDIA99",
                user_id=str(user["_id"]),
                user=user,
                client=client,
                kind="image",
            )
    assert seen_auth
    assert all(h == f"Bearer {TOKEN_A}" for h in seen_auth)


@pytest.mark.asyncio
async def test_template_sync_uses_tenant_waba_and_isolates():
    import httpx

    from app.services import meta_templates as mt

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        assert request.headers.get("Authorization") == f"Bearer {TOKEN_A}"
        return httpx.Response(200, json={"data": []})

    class _C(httpx.AsyncClient):
        def __init__(self, *a, **k):
            k["transport"] = httpx.MockTransport(handler)
            k["follow_redirects"] = False
            super().__init__(*a, **k)

    user_a = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_waba_id": WABA_A,
        "meta_connection_status": "connected",
    }
    creds_a = MetaTenantCredentials(access_token=TOKEN_A, phone_number_id=PN_A, waba_id=WABA_A)
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=creds_a),
        patch("app.services.meta_templates.httpx.AsyncClient", _C),
        patch.object(mt.settings, "META_GRAPH_VERSION", "v21.0"),
        patch.object(mt.settings, "META_WABA_ID", "999999999999999"),
    ):
        rows = await mt.fetch_graph_message_templates(user_a)
    assert rows == []
    assert WABA_A in seen[0]
    assert "999999999999999" not in seen[0]

    with patch(
        "app.services.meta_credentials.get_meta_credentials_for_user",
        side_effect=MetaCredentialsError("Meta credential phone number ID does not match this account"),
    ):
        user_b = {
            "_id": ObjectId(),
            "meta_phone_number_id": PN_B,
            "meta_waba_id": "waba-b",
            "meta_connection_status": "connected",
        }
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            await mt.fetch_graph_message_templates(user_b)


def test_campaign_and_blast_template_send_use_tenant_token():
    from app.services.whatsapp_outbound import send_whatsapp_template

    captured = {}

    class _FakeResp:
        is_error = False
        status_code = 200

        def json(self):
            return {"messages": [{"id": "wamid.C"}]}

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            captured["authorization"] = (headers or {}).get("Authorization")
            captured["url"] = url
            return _FakeResp()

    user = {
        "_id": ObjectId(),
        "meta_phone_number_id": PN_A,
        "meta_connection_status": "connected",
        "meta_waba_id": WABA_A,
    }
    creds = MetaTenantCredentials(access_token=TOKEN_A, phone_number_id=PN_A, waba_id=WABA_A)
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=creds),
        patch("app.services.meta_whatsapp_service.httpx.Client", _FakeClient),
        patch("app.services.meta_whatsapp_service.settings.META_GRAPH_VERSION", "v21.0"),
    ):
        out = send_whatsapp_template(
            provider="meta",
            to="+447700900123",
            name="order_update",
            language_code="en_US",
            user=user,
        )
    assert captured["authorization"] == f"Bearer {TOKEN_A}"
    assert PN_A in captured["url"]
    assert out["provider"] == "meta"
    assert out["provider_message_id"] == "wamid.C"