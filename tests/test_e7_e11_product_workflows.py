"""E7–E11 password reset, profile, admin, activity, notifications."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.middleware.auth import create_access_token, hash_password, verify_password
from app.services.password_reset import (
    _hash_token,
    consume_reset_token,
    generate_raw_token,
    issue_reset_token,
)
from app.services.activity import list_activity, record_activity, _safe_metadata
from app.services.notifications import create_notification, list_notifications, unread_count
from app.security.permissions import has_permission, require_permission
from fastapi import HTTPException


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def _user_doc(**kwargs):
    uid = kwargs.pop("_id", ObjectId())
    base = {
        "_id": uid,
        "email": "user@example.com",
        "password_hash": hash_password("Password1"),
        "full_name": "Test User",
        "role": "user",
        "plan": "free",
        "banned": False,
        "active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    base.update(kwargs)
    return base


# --- Password reset service ---


@pytest.mark.asyncio
async def test_reset_token_stored_hashed():
    db = MagicMock()
    db.password_reset_tokens.update_many = AsyncMock()
    db.password_reset_tokens.insert_one = AsyncMock()
    with patch("app.services.password_reset.send_email", return_value=True):
        meta = await issue_reset_token(
            db, user_id=str(ObjectId()), email="a@b.com", full_name="A"
        )
    assert "dev_reset_token" in meta
    raw = meta["dev_reset_token"]
    inserted = db.password_reset_tokens.insert_one.call_args[0][0]
    assert inserted["token_hash"] == _hash_token(raw)
    assert inserted["token_hash"] != raw
    assert "password" not in str(inserted).lower() or "password_hash" not in inserted


@pytest.mark.asyncio
async def test_consume_expired_token_rejected():
    raw = generate_raw_token()
    db = MagicMock()
    db.password_reset_tokens.find_one = AsyncMock(
        return_value={
            "_id": ObjectId(),
            "user_id": str(ObjectId()),
            "token_hash": _hash_token(raw),
            "expires_at": datetime.now(timezone.utc) - timedelta(minutes=1),
            "used_at": None,
        }
    )
    db.password_reset_tokens.update_one = AsyncMock()
    assert await consume_reset_token(db, raw_token=raw) is None


@pytest.mark.asyncio
async def test_consume_used_token_rejected():
    raw = generate_raw_token()
    db = MagicMock()
    db.password_reset_tokens.find_one = AsyncMock(return_value=None)
    assert await consume_reset_token(db, raw_token=raw) is None


@pytest.mark.asyncio
async def test_consume_malformed_token_rejected():
    db = MagicMock()
    assert await consume_reset_token(db, raw_token="short") is None


def test_forgot_password_always_generic(client):
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=None)
    with patch("app.routes.auth.get_db", return_value=db), patch(
        "app.routes.auth.rate_limit_auth"
    ), patch("app.routes.auth.rate_limit_ip"):
        res = client.post("/api/auth/forgot-password", json={"email": "nobody@example.com"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "dev_reset_token" not in body
    assert "exists" not in body["message"].lower() or "if an account exists" in body["message"].lower()


def test_forgot_password_known_user_still_generic(client):
    user = _user_doc()
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    with patch("app.routes.auth.get_db", return_value=db), patch(
        "app.routes.auth.rate_limit_auth"
    ), patch("app.routes.auth.rate_limit_ip"), patch(
        "app.routes.auth.issue_reset_token", new=AsyncMock(return_value={"ok": True})
    ):
        res = client.post("/api/auth/forgot-password", json={"email": user["email"]})
    assert res.status_code == 200
    assert "dev_reset_token" not in res.json()


@pytest.mark.asyncio
async def test_reset_password_endpoint_valid_token(client):
    uid = ObjectId()
    raw = generate_raw_token()
    token_doc = {
        "_id": ObjectId(),
        "user_id": str(uid),
        "token_hash": _hash_token(raw),
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=30),
        "used_at": None,
    }
    db = MagicMock()
    db.password_reset_tokens.find_one = AsyncMock(return_value=token_doc)
    db.password_reset_tokens.update_one = AsyncMock()
    db.password_reset_tokens.update_many = AsyncMock()
    db.users.update_one = AsyncMock()
    db.activity_events.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    db.notifications.find_one = AsyncMock(return_value=None)
    db.notifications.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))

    with patch("app.routes.auth.get_db", return_value=db), patch(
        "app.routes.auth.rate_limit_ip"
    ), patch("app.services.notifications.ws_manager.push", new=AsyncMock()):
        res = client.post(
            "/api/auth/reset-password",
            json={"token": raw, "new_password": "NewPass99"},
        )
    assert res.status_code == 200
    assert res.json()["ok"] is True
    set_fields = db.users.update_one.call_args[0][1]["$set"]
    assert "password_hash" in set_fields
    assert verify_password("NewPass99", set_fields["password_hash"])


def test_reset_weak_password_rejected(client):
    with patch("app.routes.auth.rate_limit_ip"):
        res = client.post(
            "/api/auth/reset-password",
            json={"token": "x" * 40, "new_password": "short"},
        )
    assert res.status_code == 422


def test_change_password_requires_current(client):
    user = _user_doc()
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.auth.get_db", return_value=db
    ), patch("app.routes.auth.rate_limit_user"):
        res = client.post(
            "/api/auth/change-password",
            headers={"Authorization": f"Bearer {token}"},
            json={"current_password": "WrongPass1", "new_password": "NewPass99"},
        )
    assert res.status_code == 400


def test_inactive_user_cannot_login(client):
    user = _user_doc(active=False)
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    with patch("app.routes.auth.get_db", return_value=db), patch(
        "app.routes.auth.rate_limit_auth"
    ):
        res = client.post(
            "/api/auth/login",
            json={"email": user["email"], "password": "Password1"},
        )
    assert res.status_code == 403


# --- Profile ---


def test_profile_loads_without_secrets(client):
    user = _user_doc(password_hash=hash_password("Password1"))
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    with patch("app.middleware.auth.get_db", return_value=db):
        res = client.get("/api/profile", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    body = res.json()
    assert "password" not in body
    assert "password_hash" not in body
    assert body["email"] == user["email"]


def test_profile_allowed_fields_update(client):
    user = _user_doc()
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value={**user, "full_name": "Updated"})
    db.users.update_one = AsyncMock()
    db.activity_events.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.profile.get_db", return_value=db
    ):
        res = client.patch(
            "/api/profile",
            headers={"Authorization": f"Bearer {token}"},
            json={"full_name": "Updated", "timezone": "UTC"},
        )
    assert res.status_code == 200
    assert res.json()["full_name"] == "Updated"


def test_profile_invalid_timezone_rejected(client):
    user = _user_doc()
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.profile.get_db", return_value=db
    ):
        res = client.patch(
            "/api/profile",
            headers={"Authorization": f"Bearer {token}"},
            json={"timezone": "NotARealTimezone"},
        )
    assert res.status_code == 400


def test_profile_duplicate_email_rejected(client):
    user = _user_doc()
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    from pymongo.errors import DuplicateKeyError

    db.users.update_one = AsyncMock(side_effect=DuplicateKeyError("dup"))
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.profile.get_db", return_value=db
    ):
        res = client.patch(
            "/api/profile",
            headers={"Authorization": f"Bearer {token}"},
            json={"email": "other@example.com", "current_password": "Password1"},
        )
    assert res.status_code == 409


def test_avatar_reject_bad_mime(client):
    user = _user_doc()
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.profile.rate_limit_upload"
    ):
        res = client.post(
            "/api/profile/avatar",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("x.txt", b"hello world data here", "text/plain")},
        )
    assert res.status_code == 400


# --- Admin ---


def test_normal_user_cannot_access_admin(client):
    user = _user_doc(role="user")
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    with patch("app.middleware.auth.get_db", return_value=db):
        res = client.get("/api/admin/users", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_admin_create_user(client):
    admin = _user_doc(role="admin", email="admin@example.com")
    token = create_access_token(str(admin["_id"]), "admin")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=admin)
    db.users.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    db.activity_events.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.admin.get_db", return_value=db
    ):
        res = client.post(
            "/api/admin/users",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "email": "new@example.com",
                "password": "Password1",
                "full_name": "New User",
                "role": "user",
            },
        )
    assert res.status_code == 201
    assert "password_hash" not in res.json()


def test_admin_self_deactivation_rejected(client):
    admin = _user_doc(role="admin")
    token = create_access_token(str(admin["_id"]), "admin")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=admin)
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.admin.get_db", return_value=db
    ):
        res = client.post(
            f"/api/admin/users/{admin['_id']}/deactivate",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 400


def test_last_admin_cannot_be_deactivated(client):
    admin = _user_doc(role="admin")
    target_id = ObjectId()
    target = _user_doc(_id=target_id, role="admin", email="otheradmin@example.com")
    token = create_access_token(str(admin["_id"]), "admin")
    db = MagicMock()

    async def find_one(q, *a, **k):
        if q.get("_id") == admin["_id"]:
            return admin
        if q.get("_id") == target_id:
            return target
        return None

    db.users.find_one = AsyncMock(side_effect=find_one)
    db.users.count_documents = AsyncMock(return_value=1)
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.admin.get_db", return_value=db
    ):
        res = client.post(
            f"/api/admin/users/{target_id}/deactivate",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 400


def test_admin_pagination_shape(client):
    admin = _user_doc(role="admin")
    token = create_access_token(str(admin["_id"]), "admin")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=admin)
    db.users.count_documents = AsyncMock(return_value=0)

    class _Cur:
        def sort(self, *a, **k):
            return self

        def skip(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def __aiter__(self):
            async def gen():
                if False:
                    yield None

            return gen()

    db.users.find = MagicMock(return_value=_Cur())
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.admin.get_db", return_value=db
    ):
        res = client.get(
            "/api/admin/users?page=1&page_size=10",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200
    body = res.json()
    assert "items" in body
    assert body["page"] == 1


# --- Activity / notifications ---


def test_safe_metadata_strips_secrets():
    safe = _safe_metadata(
        {"password": "x", "token": "y", "status": "ok", "message_body": "secret"}
    )
    assert "password" not in safe
    assert "token" not in safe
    assert "message_body" not in safe
    assert safe["status"] == "ok"


@pytest.mark.asyncio
async def test_activity_tenant_scoped():
    db = MagicMock()
    db.activity_events.count_documents = AsyncMock(return_value=1)

    class _Cur:
        def sort(self, *a, **k):
            return self

        def skip(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def __aiter__(self):
            async def gen():
                yield {
                    "_id": ObjectId(),
                    "tenant_id": "t1",
                    "event_type": "user.login",
                    "summary": "login",
                    "created_at": datetime.now(timezone.utc),
                    "metadata": {},
                }

            return gen()

    db.activity_events.find = MagicMock(return_value=_Cur())
    out = await list_activity(db, tenant_id="t1", page=1, page_size=10)
    assert out["total"] == 1
    db.activity_events.count_documents.assert_awaited()
    filt = db.activity_events.count_documents.call_args[0][0]
    assert filt["tenant_id"] == "t1"


@pytest.mark.asyncio
async def test_notifications_user_scoped_and_unread():
    db = MagicMock()
    db.notifications.find_one = AsyncMock(return_value=None)
    db.notifications.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    with patch("app.services.notifications.ws_manager.push", new=AsyncMock()):
        n = await create_notification(
            db,
            user_id="u1",
            type="system",
            title="Hi",
            message="Hello",
            dedupe_key="k1",
        )
    assert n is not None
    # duplicate prevented
    db.notifications.find_one = AsyncMock(return_value={"_id": ObjectId()})
    n2 = await create_notification(
        db,
        user_id="u1",
        type="system",
        title="Hi",
        message="Hello",
        dedupe_key="k1",
    )
    assert n2 is None

    db.notifications.count_documents = AsyncMock(return_value=2)

    class _Cur:
        def sort(self, *a, **k):
            return self

        def skip(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def __aiter__(self):
            async def gen():
                if False:
                    yield None

            return gen()

    db.notifications.find = MagicMock(return_value=_Cur())
    page = await list_notifications(db, user_id="u1", unread_only=True)
    assert page["total"] == 2
    filt = db.notifications.count_documents.call_args[0][0]
    assert filt["user_id"] == "u1"
    assert filt["is_read"] is False
    assert await unread_count(db, user_id="u1") == 2


def test_permissions_manage_users_admin_only():
    assert has_permission({"role": "admin"}, "manage_users")
    assert not has_permission({"role": "user"}, "manage_users")
    with pytest.raises(HTTPException):
        require_permission({"role": "agent"}, "manage_users")


def test_cross_user_token_misuse(client):
    """Token belonging to user A cannot reset if consume returns None (wrong hash)."""
    with patch("app.routes.auth.rate_limit_ip"), patch(
        "app.routes.auth.consume_reset_token", new=AsyncMock(return_value=None)
    ):
        res = client.post(
            "/api/auth/reset-password",
            json={"token": "a" * 40, "new_password": "NewPass99"},
        )
    assert res.status_code == 400


def test_activity_endpoint_tenant(client):
    user = _user_doc()
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    db.activity_events.count_documents = AsyncMock(return_value=0)

    class _Cur:
        def sort(self, *a, **k):
            return self

        def skip(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def __aiter__(self):
            async def gen():
                if False:
                    yield None

            return gen()

    db.activity_events.find = MagicMock(return_value=_Cur())
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.activity.get_db", return_value=db
    ):
        res = client.get("/api/activity", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["items"] == []


def test_notifications_mark_read(client):
    user = _user_doc()
    nid = ObjectId()
    token = create_access_token(str(user["_id"]), "user")
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    db.notifications.update_one = AsyncMock(return_value=SimpleNamespace(modified_count=1))
    with patch("app.middleware.auth.get_db", return_value=db), patch(
        "app.routes.notifications.get_db", return_value=db
    ):
        res = client.post(
            f"/api/notifications/{nid}/read",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert res.status_code == 200


def test_dev_issue_endpoint_returns_token_in_dev(client):
    user = _user_doc()
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=user)
    with patch("app.routes.auth.get_db", return_value=db), patch(
        "app.routes.auth.rate_limit_ip"
    ), patch(
        "app.routes.auth.issue_reset_token",
        new=AsyncMock(return_value={"ok": True, "dev_reset_token": "tok12345678901234567890"}),
    ):
        # Only when APP_ENV is dev/test
        assert settings.is_dev_or_test
        res = client.post(
            "/api/auth/forgot-password/dev-issue",
            json={"email": user["email"]},
        )
    assert res.status_code == 200
    assert "dev_reset_token" in res.json()
