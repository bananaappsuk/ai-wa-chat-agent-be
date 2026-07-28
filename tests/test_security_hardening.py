"""Security hardening tests (auth, tenant isolation, validation, headers, health)."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from jose import jwt

from app.config import Settings, get_settings
from app.main import app
from app.middleware.auth import create_access_token, hash_password
from app.security.ssrf import assert_safe_remote_media_url
from app.security.validation import (
    is_safe_http_url,
    reject_mongo_operators,
    require_object_id,
    validate_password_complexity,
)
from fastapi import HTTPException


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def test_health_has_no_secrets(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body.get("ok") is True
    blob = str(body).lower()
    assert "token" not in blob
    assert "password" not in blob
    assert "secret" not in blob


def test_security_headers_present(client):
    res = client.get("/health")
    assert res.headers.get("X-Content-Type-Options") == "nosniff"
    assert res.headers.get("X-Frame-Options") == "DENY"
    assert "X-Request-ID" in res.headers
    assert "Referrer-Policy" in res.headers


def test_expired_jwt_rejected():
    from app.config import settings

    payload = {
        "sub": str(ObjectId()),
        "role": "user",
        "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
        "typ": "access",
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALG)
    from app.middleware.auth import decode_token

    with pytest.raises(HTTPException) as exc:
        decode_token(token)
    assert exc.value.status_code == 401


def test_malformed_jwt_rejected():
    from app.middleware.auth import decode_token

    with pytest.raises(HTTPException) as exc:
        decode_token("not.a.jwt")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_user_rejected():
    from app.middleware.auth import _user_from_token

    token = create_access_token(str(ObjectId()), "user")
    with patch("app.middleware.auth.get_db") as gdb:
        gdb.return_value.users.find_one = AsyncMock(return_value=None)
        with pytest.raises(HTTPException) as exc:
            await _user_from_token(token)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_banned_user_rejected():
    from app.middleware.auth import _user_from_token

    uid = ObjectId()
    token = create_access_token(str(uid), "user")
    with patch("app.middleware.auth.get_db") as gdb:
        gdb.return_value.users.find_one = AsyncMock(
            return_value={"_id": uid, "banned": True, "role": "user"}
        )
        with pytest.raises(HTTPException) as exc:
            await _user_from_token(token)
    assert exc.value.status_code == 403


def test_weak_password_rejected():
    with pytest.raises(HTTPException):
        validate_password_complexity("short1")
    with pytest.raises(HTTPException):
        validate_password_complexity("allletters")
    with pytest.raises(HTTPException):
        validate_password_complexity("12345678")
    validate_password_complexity("GoodPass1")


def test_unsafe_url_schemes_rejected():
    assert not is_safe_http_url("javascript:alert(1)")
    assert not is_safe_http_url("data:text/html,hi")
    assert not is_safe_http_url("file:///etc/passwd")
    assert is_safe_http_url("https://example.com/a")


def test_nosql_operator_rejected():
    with pytest.raises(HTTPException):
        reject_mongo_operators({"$gt": 1})
    with pytest.raises(HTTPException):
        reject_mongo_operators({"name": {"$ne": "x"}})
    reject_mongo_operators({"name": "ok", "tags": ["a"]})


def test_invalid_object_id_rejected():
    with pytest.raises(HTTPException) as exc:
        require_object_id("not-an-id")
    assert exc.value.status_code == 404


def test_ssrf_blocks_localhost_and_private():
    with pytest.raises(HTTPException):
        assert_safe_remote_media_url("http://api.twilio.com/x")
    with pytest.raises(HTTPException):
        assert_safe_remote_media_url("https://evil.example.com/x")
    with pytest.raises(HTTPException):
        assert_safe_remote_media_url("https://127.0.0.1/x")


def test_ssrf_allows_twilio_https(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("104.16.0.1", 443))],
    )
    url = assert_safe_remote_media_url("https://api.twilio.com/2010-04-01/Accounts/ACxxx/Messages/MMxxx/Media/MExxx")
    assert url.startswith("https://api.twilio.com/")


def test_production_rejects_weak_jwt():
    s = Settings(
        APP_ENV="production",
        MONGO_URI="mongodb://x",
        MONGO_DB="db",
        JWT_SECRET="change-me",
        REDIS_URL="rediss://x",
        TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        TWILIO_AUTH_TOKEN="token",
        TWILIO_WHATSAPP_FROM="whatsapp:+10000000000",
        PUBLIC_BASE_URL="https://api.example.com",
        CORS_ORIGINS="https://app.example.com",
        TWILIO_VALIDATE_SIGNATURE=True,
        OPENAI_API_KEY="sk-test",
        AI_FEATURES_ENABLED=True,
    )
    with pytest.raises(RuntimeError):
        s.validate_for_startup()


def test_login_rate_limit_enforced(client):
    from app.security import rate_limit as rl

    calls = {"n": 0}

    def fake_check(*, key, limit, window_sec):
        calls["n"] += 1
        if calls["n"] > 2:
            raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    with (
        patch.object(rl, "check_rate_limit", side_effect=fake_check),
        patch("app.routes.auth.get_db") as gdb,
    ):
        gdb.return_value.users.find_one = AsyncMock(return_value=None)
        r1 = client.post("/api/auth/login", json={"email": "a@b.com", "password": "x"})
        r2 = client.post("/api/auth/login", json={"email": "a@b.com", "password": "x"})
        r3 = client.post("/api/auth/login", json={"email": "a@b.com", "password": "x"})
    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 429


@pytest.mark.asyncio
async def test_tenant_cannot_access_other_lead():
    from app.services import lead_service

    with patch("app.services.lead_service.get_db") as gdb:
        gdb.return_value.leads.find_one = AsyncMock(return_value=None)
        doc = await lead_service.get_lead(str(ObjectId()), str(ObjectId()))
    assert doc is None


@pytest.mark.asyncio
async def test_admin_route_requires_admin(client):
    uid = ObjectId()
    token = create_access_token(str(uid), "user")
    with patch("app.middleware.auth.get_db") as gdb:
        gdb.return_value.users.find_one = AsyncMock(
            return_value={"_id": uid, "banned": False, "role": "user", "email": "u@x.com"}
        )
        res = client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_media_path_traversal_rejected(tmp_path):
    from app.routes import media as media_route
    from app.services.media_storage import LocalMediaStorage

    storage = LocalMediaStorage(str(tmp_path))
    with patch.object(media_route, "get_media_storage", return_value=storage):
        with pytest.raises(HTTPException) as exc:
            await media_route.serve_media_file("../../etc/passwd")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_upload_rejects_html_double_ext(tmp_path):
    from app.routes import media as media_route
    from app.services.media_storage import LocalMediaStorage

    user = {"_id": ObjectId()}
    upload = MagicMock()
    upload.filename = "photo.jpg.html"
    upload.content_type = "image/jpeg"
    upload.read = AsyncMock(return_value=b"\xff\xd8\xff\xe0" + b"\x00" * 20)

    with patch.object(media_route, "get_media_storage", return_value=LocalMediaStorage(str(tmp_path))):
        with pytest.raises(HTTPException) as exc:
            await media_route.upload_media(file=upload, user=user)
    assert exc.value.status_code == 400


def test_dev_signature_bypass_only_when_configured():
    from app.services import twilio_service
    from app.config import settings

    with patch.object(settings, "TWILIO_VALIDATE_SIGNATURE", False), patch.object(
        settings, "TWILIO_VALIDATE_SIGNATURES", False
    ):
        assert twilio_service.validate_signature("https://x", {}, "") is True


def test_invalid_signature_rejected_when_enabled():
    from app.services import twilio_service
    from app.config import settings

    with (
        patch.object(settings, "TWILIO_VALIDATE_SIGNATURE", True),
        patch.object(settings, "TWILIO_VALIDATE_SIGNATURES", True),
        patch.object(settings, "TWILIO_AUTH_TOKEN", "tok"),
        patch.object(twilio_service, "_validator") as v,
    ):
        v.return_value.validate.return_value = False
        assert twilio_service.validate_signature("https://x", {"a": "1"}, "sig") is False


@pytest.mark.asyncio
async def test_ws_unauthenticated_closes(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat?token="):
            pass


def test_password_hash_not_in_user_out():
    from app.models.user import UserOut

    u = UserOut(
        id=str(ObjectId()),
        email="a@b.com",
        full_name="A",
        plan="free",
        role="user",
        banned=False,
    )
    dumped = u.model_dump()
    assert "password" not in dumped
    assert "password_hash" not in dumped


def test_hash_password_uses_bcrypt():
    h = hash_password("GoodPass1")
    assert h.startswith("$2")
    assert "GoodPass1" not in h
