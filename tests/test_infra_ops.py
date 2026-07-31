"""A4–A12 infrastructure / ops tests."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.security.audit import sanitize_error_message


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def test_health_is_lightweight(client):
    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "mongo" not in body
    assert "redis" not in body
    assert "token" not in str(body).lower()


def test_ready_reports_dependency_failure(client):
    with (
        patch("app.db.mongo.get_client") as gc,
        patch("app.workers.queue.get_redis") as gr,
    ):
        gc.return_value.admin.command = pytest.raises  # type: ignore
        # Make mongo fail
        async def boom(*a, **k):
            raise RuntimeError("mongo down")

        gc.return_value.admin.command = boom
        gr.return_value.ping.side_effect = RuntimeError("redis down")
        # get_client().admin.command is awaited — need AsyncMock style
        from unittest.mock import AsyncMock, MagicMock

        client_mock = MagicMock()
        client_mock.admin.command = AsyncMock(side_effect=RuntimeError("mongo down"))
        gc.return_value = client_mock
        redis_mock = MagicMock()
        redis_mock.ping.side_effect = RuntimeError("redis down")
        gr.return_value = redis_mock

        res = client.get("/ready")
    assert res.status_code == 503
    assert res.json()["ok"] is False


def test_logging_redacts_secrets():
    msg = sanitize_error_message("Authorization Bearer abcdef.ghij.klmn password=secret123")
    assert "abcdef" not in msg or "REDACTED" in msg
    assert "secret123" not in msg or "REDACTED" in msg


def test_sentry_disabled_without_dsn(monkeypatch):
    from app.observability import sentry as sentry_mod

    monkeypatch.setattr(sentry_mod.settings, "SENTRY_DSN", "")
    sentry_mod._INITIALIZED = False
    assert sentry_mod.init_sentry(service="api") is False


def test_metrics_requires_token_when_configured(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "METRICS_ENABLED", True)
    monkeypatch.setattr(settings, "METRICS_TOKEN", "secret-metrics")
    res = client.get("/metrics")
    assert res.status_code == 401
    res_ok = client.get("/metrics", headers={"X-Metrics-Token": "secret-metrics"})
    assert res_ok.status_code == 200
    assert b"http_requests_total" in res_ok.content or b"prometheus" in res_ok.content.lower() or res_ok.headers.get("content-type")


def test_metrics_disabled_returns_404(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "METRICS_ENABLED", False)
    res = client.get("/metrics")
    assert res.status_code == 404


def test_metrics_enabled_without_token_is_unauthorized(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "METRICS_ENABLED", True)
    monkeypatch.setattr(settings, "METRICS_TOKEN", "")
    res = client.get("/metrics")
    assert res.status_code == 401


def test_inline_scheduler_default_off_in_production():
    s = Settings(
        APP_ENV="production",
        MONGO_URI="mongodb://x",
        MONGO_DB="db",
        JWT_SECRET="x" * 32,
        REDIS_URL="rediss://x",
        TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        TWILIO_AUTH_TOKEN="tok",
        TWILIO_WHATSAPP_FROM="whatsapp:+10000000000",
        PUBLIC_BASE_URL="https://api.example.com",
        CORS_ORIGINS="https://app.example.com",
        TWILIO_VALIDATE_SIGNATURE=True,
        OPENAI_API_KEY="sk-test",
        AI_FEATURES_ENABLED=True,
        RUN_INLINE_SCHEDULER=None,
    )
    assert s.run_inline_scheduler is False


def test_inline_scheduler_default_on_in_dev():
    s = Settings(APP_ENV="dev", RUN_INLINE_SCHEDULER=None)
    assert s.run_inline_scheduler is True


def test_mongo_timeout_settings_present():
    s = Settings(APP_ENV="dev")
    assert s.MONGO_CONNECT_TIMEOUT_MS >= 1000
    assert s.MONGO_SERVER_SELECTION_TIMEOUT_MS >= 1000
    assert s.REDIS_CONNECT_TIMEOUT_SECONDS >= 1


def test_scheduler_lock_acquire(monkeypatch):
    from scheduler import _acquire_lock, _OWNER

    class FakeRedis:
        def __init__(self):
            self.store = {}

        def set(self, key, value, nx=False, ex=None):
            if nx and key in self.store:
                return False
            self.store[key] = value
            return True

        def get(self, key):
            return self.store.get(key)

        def expire(self, key, ttl):
            return True

        def delete(self, key):
            self.store.pop(key, None)

    r = FakeRedis()
    assert _acquire_lock(r) is True
    # Second acquire as same process would overwrite only if not nx — simulate other owner
    r.store["locks:campaign_scheduler"] = "other"
    assert _acquire_lock(r) is False


def test_production_rejects_reload_unsafe_jwt():
    s = Settings(
        APP_ENV="production",
        MONGO_URI="mongodb://x",
        MONGO_DB="db",
        JWT_SECRET="change-me",
        REDIS_URL="rediss://x",
        TWILIO_ACCOUNT_SID="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
        TWILIO_AUTH_TOKEN="tok",
        TWILIO_WHATSAPP_FROM="whatsapp:+10000000000",
        PUBLIC_BASE_URL="https://api.example.com",
        CORS_ORIGINS="https://app.example.com",
        TWILIO_VALIDATE_SIGNATURE=True,
        OPENAI_API_KEY="sk",
        AI_FEATURES_ENABLED=True,
    )
    with pytest.raises(RuntimeError):
        s.validate_for_startup()
