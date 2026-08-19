"""Phase 1 Meta WhatsApp Cloud API POC tests (mocked Graph API; no real sends)."""
from __future__ import annotations

import hashlib
import hmac
import json
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.services import meta_whatsapp_service


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


SAMPLE_INBOUND = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "WABA_ID",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15550001111",
                            "phone_number_id": "PHONE_NUMBER_ID",
                        },
                        "contacts": [{"profile": {"name": "Test"}, "wa_id": "447700900123"}],
                        "messages": [
                            {
                                "from": "447700900123",
                                "id": "wamid.TEST_INBOUND_1",
                                "timestamp": "1710000000",
                                "type": "text",
                                "text": {"body": "hello meta"},
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}

SAMPLE_STATUS = {
    "object": "whatsapp_business_account",
    "entry": [
        {
            "id": "WABA_ID",
            "changes": [
                {
                    "value": {
                        "messaging_product": "whatsapp",
                        "metadata": {
                            "display_phone_number": "15550001111",
                            "phone_number_id": "PHONE_NUMBER_ID",
                        },
                        "statuses": [
                            {
                                "id": "wamid.TEST_STATUS_1",
                                "status": "delivered",
                                "timestamp": "1710000001",
                                "recipient_id": "447700900123",
                            }
                        ],
                    },
                    "field": "messages",
                }
            ],
        }
    ],
}


def _sign(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_webhook_get_verification_succeeds(client):
    with patch.object(
        meta_whatsapp_service.settings,
        "META_WEBHOOK_VERIFY_TOKEN",
        "poc-verify-token",
    ):
        res = client.get(
            "/api/webhook/meta/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "poc-verify-token",
                "hub.challenge": "12345challenge",
            },
        )
    assert res.status_code == 200
    assert res.text == "12345challenge"


def test_webhook_get_verification_fails_wrong_token(client):
    with patch.object(
        meta_whatsapp_service.settings,
        "META_WEBHOOK_VERIFY_TOKEN",
        "poc-verify-token",
    ):
        res = client.get(
            "/api/webhook/meta/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "12345challenge",
            },
        )
    assert res.status_code == 403


def test_parse_inbound_text_payload():
    msgs = meta_whatsapp_service.parse_inbound_messages(SAMPLE_INBOUND)
    assert len(msgs) == 1
    msg = msgs[0]
    assert msg.provider == "meta"
    assert msg.provider_message_id == "wamid.TEST_INBOUND_1"
    assert msg.phone_number_id == "PHONE_NUMBER_ID"
    assert msg.from_number == "+447700900123"
    assert msg.message_type == "text"
    assert msg.text == "hello meta"


def test_parse_status_payload():
    statuses = meta_whatsapp_service.parse_status_updates(SAMPLE_STATUS)
    assert len(statuses) == 1
    st = statuses[0]
    assert st.provider == "meta"
    assert st.provider_message_id == "wamid.TEST_STATUS_1"
    assert st.status == "delivered"
    assert st.recipient_id == "447700900123"
    assert st.phone_number_id == "PHONE_NUMBER_ID"


def test_invalid_webhook_signature_rejected(client):
    body = json.dumps(SAMPLE_INBOUND).encode("utf-8")
    with patch.object(meta_whatsapp_service.settings, "META_WEBHOOK_VALIDATE_SIGNATURE", True), patch.object(
        meta_whatsapp_service.settings, "META_APP_SECRET", "app-secret"
    ):
        res = client.post(
            "/api/webhook/meta/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=deadbeef",
            },
        )
    assert res.status_code == 403


def test_valid_webhook_signature_parses_inbound(client):
    body = json.dumps(SAMPLE_INBOUND).encode("utf-8")
    secret = "app-secret"
    with patch.object(meta_whatsapp_service.settings, "META_WEBHOOK_VALIDATE_SIGNATURE", True), patch.object(
        meta_whatsapp_service.settings, "META_APP_SECRET", secret
    ):
        res = client.post(
            "/api/webhook/meta/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body, secret),
            },
        )
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["messages"] == 1
    assert data["statuses"] == 0


def test_valid_webhook_signature_parses_status(client):
    body = json.dumps(SAMPLE_STATUS).encode("utf-8")
    secret = "app-secret"
    with patch.object(meta_whatsapp_service.settings, "META_WEBHOOK_VALIDATE_SIGNATURE", True), patch.object(
        meta_whatsapp_service.settings, "META_APP_SECRET", secret
    ):
        res = client.post(
            "/api/webhook/meta/whatsapp",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": _sign(body, secret),
            },
        )
    assert res.status_code == 200
    assert res.json()["statuses"] == 1


def test_send_text_builds_correct_request_and_mocks_graph():
    captured: dict = {}

    class _FakeResp:
        status_code = 200
        is_error = False
        text = ""

        def json(self):
            return {
                "messaging_product": "whatsapp",
                "contacts": [{"input": "447700900123", "wa_id": "447700900123"}],
                "messages": [{"id": "wamid.OUTBOUND_1"}],
            }

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json=None, headers=None):
            captured["url"] = url
            captured["authorization"] = (headers or {}).get("Authorization")
            captured["json"] = json
            return _FakeResp()

    with patch.object(meta_whatsapp_service.settings, "META_ACCESS_TOKEN", "test-token"), patch.object(
        meta_whatsapp_service.settings, "META_PHONE_NUMBER_ID", "123456789"
    ), patch.object(meta_whatsapp_service.settings, "META_GRAPH_VERSION", "v21.0"), patch(
        "app.services.meta_whatsapp_service.httpx.Client", _FakeClient
    ):
        result = meta_whatsapp_service.send_text(to="+447700900123", text="Hello from Meta Cloud API")

    assert result.provider == "meta"
    assert result.provider_message_id == "wamid.OUTBOUND_1"
    assert result.to == "447700900123"
    assert captured["url"] == "https://graph.facebook.com/v21.0/123456789/messages"
    assert captured["authorization"] == "Bearer test-token"
    assert captured["json"]["messaging_product"] == "whatsapp"
    assert captured["json"]["to"] == "447700900123"
    assert captured["json"]["type"] == "text"
    assert captured["json"]["text"]["body"] == "Hello from Meta Cloud API"



def test_meta_test_send_requires_auth(client):
    res = client.post("/api/meta/test-send", json={"to": "+447700900123", "message": "hi"})
    assert res.status_code == 401


def test_whatsapp_provider_defaults_to_twilio():
    assert Settings.model_fields["WHATSAPP_PROVIDER"].default == "twilio"
