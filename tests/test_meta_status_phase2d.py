"""Phase 2D Meta outbound delivery/read/failed status persistence (no live Graph)."""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.main import app
from app.services import meta_whatsapp_service, status_callback
from app.services.status_callback import apply_meta_status_update
from tests.test_meta_inbound_phase2a import MemDB, _post_meta, _user
from tests.test_meta_whatsapp_poc import SAMPLE_STATUS


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def _now():
    return datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)


def _outbound(*, user_id: str, wamid: str, status: str = "queued", extra=None) -> dict:
    doc = {
        "_id": ObjectId(),
        "user_id": user_id,
        "lead_id": str(ObjectId()),
        "direction": "outbound",
        "message": "hello",
        "status": status,
        "provider": "meta",
        "provider_message_id": wamid,
        "sender_type": "human",
        "created_at": _now(),
    }
    if extra:
        doc.update(extra)
    return doc


@contextmanager
def _env_pnid(value: str):
    from app.config import settings as cfg

    with patch.object(cfg, "META_PHONE_NUMBER_ID", value):
        yield


async def _apply(mem, wamid, status, *, errors=None, pnid="PN_A", publish=None):
    pub = publish if publish is not None else MagicMock()
    with (
        patch.object(status_callback, "_publish_best_effort", pub),
        _env_pnid("PN_A"),
    ):
        result = await apply_meta_status_update(
            provider_message_id=wamid,
            status_raw=status,
            errors=errors,
            phone_number_id=pnid,
            db=mem,
        )
    return result, pub


def test_parser_includes_phone_number_id():
    statuses = meta_whatsapp_service.parse_status_updates(SAMPLE_STATUS)
    assert statuses[0].phone_number_id == "PHONE_NUMBER_ID"
    assert statuses[0].provider_message_id == "wamid.TEST_STATUS_1"


@pytest.mark.asyncio
async def test_queued_to_sent():
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    mem.messages.docs.append(_outbound(user_id=str(user["_id"]), wamid="wamid.S1", status="queued"))
    result, pub = await _apply(mem, "wamid.S1", "sent")
    assert result["updated"] is True
    assert mem.messages.docs[0]["status"] == "sent"
    assert mem.messages.docs[0].get("sent_at")
    pub.assert_called_once()
    assert pub.call_args.args[0] == str(user["_id"])
    assert pub.call_args.args[1] == "message:updated"


@pytest.mark.asyncio
async def test_sent_to_delivered_to_read():
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    uid = str(user["_id"])
    mem.messages.docs.append(_outbound(user_id=uid, wamid="wamid.D1", status="sent"))
    r1, _ = await _apply(mem, "wamid.D1", "delivered")
    assert r1["updated"] is True
    assert mem.messages.docs[0]["status"] == "delivered"
    assert mem.messages.docs[0].get("delivered_at")
    r2, _ = await _apply(mem, "wamid.D1", "read")
    assert r2["updated"] is True
    assert mem.messages.docs[0]["status"] == "read"
    assert mem.messages.docs[0].get("read_at")


@pytest.mark.asyncio
async def test_no_regression_and_duplicate_noop():
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    uid = str(user["_id"])
    mem.messages.docs.append(
        _outbound(user_id=uid, wamid="wamid.N1", status="delivered", extra={"delivered_at": _now()})
    )
    r1, pub1 = await _apply(mem, "wamid.N1", "sent")
    assert r1["updated"] is False
    assert mem.messages.docs[0]["status"] == "delivered"
    pub1.assert_not_called()

    mem.messages.docs[0]["status"] = "read"
    r2, pub2 = await _apply(mem, "wamid.N1", "delivered")
    assert r2["updated"] is False
    assert mem.messages.docs[0]["status"] == "read"
    pub2.assert_not_called()

    r3, pub3 = await _apply(mem, "wamid.N1", "read")
    assert r3["updated"] is False
    pub3.assert_not_called()


@pytest.mark.asyncio
async def test_failed_stores_safe_error_fields():
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    mem.messages.docs.append(_outbound(user_id=str(user["_id"]), wamid="wamid.F1", status="sent"))
    raw_errors = [
        {
            "code": 131026,
            "title": "Message undeliverable",
            "message": "Re-engagement message",
            "error_data": {"details": "secret-ish"},
        }
    ]
    result, pub = await _apply(mem, "wamid.F1", "failed", errors=raw_errors)
    assert result["updated"] is True
    doc = mem.messages.docs[0]
    assert doc["status"] == "failed"
    assert doc["error_code"] == "131026"
    assert doc["error"] == "Message undeliverable"
    assert doc["error_message"] == "Message undeliverable"
    assert doc.get("failed_at")
    assert "errors" not in doc
    dumped = str(doc)
    assert "secret-ish" not in dumped
    pub.assert_called_once()


@pytest.mark.asyncio
async def test_unknown_and_empty_wamid_no_write():
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    mem.messages.docs.append(_outbound(user_id=str(user["_id"]), wamid="wamid.KEEP", status="sent"))
    r1, pub1 = await _apply(mem, "wamid.MISSING", "delivered")
    assert r1["updated"] is False
    assert r1["reason"] == "unknown"
    assert mem.messages.docs[0]["status"] == "sent"
    pub1.assert_not_called()

    r2, pub2 = await _apply(mem, "", "delivered")
    assert r2["reason"] == "empty_id"
    pub2.assert_not_called()
    assert len(mem.messages.docs) == 1


@pytest.mark.asyncio
async def test_twilio_and_inbound_rows_cannot_match():
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    uid = str(user["_id"])
    mem.messages.docs.append(
        {
            "_id": ObjectId(),
            "user_id": uid,
            "direction": "outbound",
            "status": "queued",
            "provider": "twilio",
            "twilio_sid": "wamid.SAME",
            "provider_message_id": "wamid.SAME",
            "message": "twilio",
        }
    )
    mem.messages.docs.append(
        {
            "_id": ObjectId(),
            "user_id": uid,
            "direction": "inbound",
            "status": "received",
            "provider": "meta",
            "provider_message_id": "wamid.IN1",
            "message": "hi",
        }
    )
    r1, _ = await _apply(mem, "wamid.SAME", "delivered")
    assert r1["reason"] == "unknown"
    assert mem.messages.docs[0]["status"] == "queued"
    r2, _ = await _apply(mem, "wamid.IN1", "delivered")
    assert r2["reason"] == "unknown"
    assert mem.messages.docs[1]["status"] == "received"


@pytest.mark.asyncio
async def test_pnid_mismatch_skips_missing_user_pnid_allows():
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    mem.messages.docs.append(_outbound(user_id=str(user["_id"]), wamid="wamid.P1", status="sent"))
    r1, pub1 = await _apply(mem, "wamid.P1", "delivered", pnid="PN_OTHER")
    assert r1["reason"] == "pnid_mismatch"
    assert mem.messages.docs[0]["status"] == "sent"
    pub1.assert_not_called()

    user2 = {"_id": ObjectId(), "email": "nopnid@example.com"}
    mem.users.docs.append(user2)
    mem.messages.docs.append(_outbound(user_id=str(user2["_id"]), wamid="wamid.P2", status="sent"))
    r2, pub2 = await _apply(mem, "wamid.P2", "delivered", pnid="PN_OTHER")
    assert r2["updated"] is True
    assert mem.messages.docs[1]["status"] == "delivered"
    assert pub2.call_args.args[0] == str(user2["_id"])


def _status_payload(*, pnid: str, wamid: str, status: str, extra_status=None) -> dict:
    statuses = [
        {
            "id": wamid,
            "status": status,
            "timestamp": "1710000001",
            "recipient_id": "447700900123",
        }
    ]
    if extra_status is not None:
        statuses.append(extra_status)
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
                                "display_phone_number": "15550001111",
                                "phone_number_id": pnid,
                            },
                            "statuses": statuses,
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def test_status_only_webhook_no_lead_creation(client):
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    mem.messages.docs.append(_outbound(user_id=str(user["_id"]), wamid="wamid.W1", status="queued"))
    publish = MagicMock()
    with (
        patch("app.routes.meta_webhook.get_db", return_value=mem),
        patch.object(status_callback, "_publish_best_effort", publish),
        patch("app.security.rate_limit.rate_limit_webhook"),
        _env_pnid("PN_A"),
    ):
        res = _post_meta(client, _status_payload(pnid="PN_A", wamid="wamid.W1", status="sent"))
    assert res.status_code == 200
    assert res.json()["statuses"] == 1
    assert res.json()["messages"] == 0
    assert len(mem.leads.docs) == 0
    assert len(mem.messages.docs) == 1
    assert mem.messages.docs[0]["status"] == "sent"
    publish.assert_called_once()
    assert publish.call_args.args[0] == str(user["_id"])


def test_malformed_status_does_not_abort_and_exception_stays_200(client):
    mem = MemDB()
    user = _user("PN_A")
    mem.users.docs.append(user)
    mem.messages.docs.append(_outbound(user_id=str(user["_id"]), wamid="wamid.OK", status="queued"))
    payload = _status_payload(
        pnid="PN_A",
        wamid="wamid.OK",
        status="sent",
        extra_status="not-a-dict",
    )
    publish = MagicMock()
    with (
        patch("app.routes.meta_webhook.get_db", return_value=mem),
        patch.object(status_callback, "_publish_best_effort", publish),
        patch("app.security.rate_limit.rate_limit_webhook"),
        _env_pnid("PN_A"),
    ):
        res = _post_meta(client, payload)
    assert res.status_code == 200
    assert mem.messages.docs[0]["status"] == "sent"

    with (
        patch("app.routes.meta_webhook.get_db", return_value=mem),
        patch(
            "app.routes.meta_webhook.apply_meta_status_update",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
        patch("app.security.rate_limit.rate_limit_webhook"),
        _env_pnid("PN_A"),
    ):
        res2 = _post_meta(client, _status_payload(pnid="PN_A", wamid="wamid.OK", status="delivered"))
    assert res2.status_code == 200
