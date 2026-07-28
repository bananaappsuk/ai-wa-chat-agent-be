"""HTTP tests for Twilio delivery status callback route."""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.main import app
from app.models.common import utcnow


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


def _empty_find():
    return MagicMock(return_value=MagicMock(to_list=AsyncMock(return_value=[])))


def _db_with_messages(messages_coll, blast_recipients=None):
    db = MagicMock()
    db.messages = messages_coll
    if blast_recipients is None:
        blast_recipients = MagicMock()
        blast_recipients.find = _empty_find()
        blast_recipients.update_one = AsyncMock()
    db.blast_recipients = blast_recipients
    camp = MagicMock()
    camp.find = _empty_find()
    camp.update_one = AsyncMock()
    db.campaign_recipients = camp
    db.campaigns = MagicMock()
    db.campaigns.update_one = AsyncMock()
    db.campaigns.find_one = AsyncMock(return_value=None)
    return db


def test_invalid_signature_returns_403(client):
    with patch("app.routes.webhook.twilio_service.validate_signature", return_value=False):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SM1", "MessageStatus": "sent"},
            headers={"X-Twilio-Signature": "bad"},
        )
    assert res.status_code == 403


def test_missing_sid_returns_200(client):
    with patch("app.routes.webhook.twilio_service.validate_signature", return_value=True):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageStatus": "sent"},
        )
    assert res.status_code == 200


def test_sent_callback_updates_message(client):
    msg_id = ObjectId()
    user_id = str(ObjectId())
    doc = {
        "_id": msg_id,
        "user_id": user_id,
        "lead_id": str(ObjectId()),
        "direction": "outbound",
        "message": "hi",
        "status": "queued",
        "twilio_sid": "SMabc",
        "created_at": utcnow(),
    }

    messages_coll = MagicMock()
    messages_coll.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[doc]))
    )
    messages_coll.update_one = AsyncMock()
    db = _db_with_messages(messages_coll)

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
        patch("app.services.status_callback._publish_best_effort") as publish,
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SMabc", "MessageStatus": "sent"},
        )

    assert res.status_code == 200
    assert messages_coll.update_one.await_count == 1
    publish.assert_called()
    args = publish.call_args[0]
    assert args[0] == user_id
    assert args[1] == "message:updated"
    assert args[2]["status"] == "sent"


def test_delivered_then_sent_stays_delivered(client):
    msg_id = ObjectId()
    doc = {
        "_id": msg_id,
        "user_id": str(ObjectId()),
        "lead_id": str(ObjectId()),
        "status": "delivered",
        "twilio_sid": "SMxyz",
        "delivered_at": utcnow(),
        "sent_at": utcnow(),
    }
    messages_coll = MagicMock()
    messages_coll.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[doc]))
    )
    messages_coll.update_one = AsyncMock()
    db = _db_with_messages(messages_coll)

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
        patch("app.services.status_callback._publish_best_effort") as publish,
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SMxyz", "MessageStatus": "sent"},
        )

    assert res.status_code == 200
    assert messages_coll.update_one.await_count == 0
    publish.assert_not_called()


def test_read_callback_updates_read_at(client):
    msg_id = ObjectId()
    doc = {
        "_id": msg_id,
        "user_id": str(ObjectId()),
        "lead_id": str(ObjectId()),
        "status": "delivered",
        "twilio_sid": "SMread",
        "delivered_at": utcnow(),
        "sent_at": utcnow(),
    }
    messages_coll = MagicMock()
    messages_coll.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[doc]))
    )
    messages_coll.update_one = AsyncMock()
    db = _db_with_messages(messages_coll)

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
        patch("app.services.status_callback._publish_best_effort"),
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SMread", "MessageStatus": "read"},
        )

    assert res.status_code == 200
    set_fields = messages_coll.update_one.await_args[0][1]["$set"]
    assert set_fields["status"] == "read"
    assert "read_at" in set_fields


def test_unknown_sid_returns_200_no_update(client):
    messages_coll = MagicMock()
    messages_coll.find = _empty_find()
    messages_coll.update_one = AsyncMock()
    blast_recipients = MagicMock()
    blast_recipients.find = _empty_find()
    blast_recipients.update_one = AsyncMock()
    db = _db_with_messages(messages_coll, blast_recipients)

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SMunknown", "MessageStatus": "delivered"},
        )

    assert res.status_code == 200
    assert messages_coll.update_one.await_count == 0
    assert blast_recipients.update_one.await_count == 0


def test_failure_callback_stores_error(client):
    msg_id = ObjectId()
    doc = {
        "_id": msg_id,
        "user_id": str(ObjectId()),
        "lead_id": str(ObjectId()),
        "status": "sent",
        "twilio_sid": "SMfail",
    }
    messages_coll = MagicMock()
    messages_coll.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[doc]))
    )
    messages_coll.update_one = AsyncMock()
    db = _db_with_messages(messages_coll)

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
        patch("app.services.status_callback._publish_best_effort"),
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={
                "MessageSid": "SMfail",
                "MessageStatus": "failed",
                "ErrorCode": "30003",
                "ErrorMessage": "Unreachable",
            },
        )

    assert res.status_code == 200
    set_fields = messages_coll.update_one.await_args[0][1]["$set"]
    assert set_fields["status"] == "failed"
    assert set_fields["error_code"] == "30003"
    assert "Unreachable" in set_fields["error"]


def test_blast_recipient_callback_updates_metrics(client):
    rid = ObjectId()
    bid = ObjectId()
    user_id = str(ObjectId())
    recipient = {
        "_id": rid,
        "blast_id": str(bid),
        "phone": "+15551212",
        "status": "sent",
        "twilio_sid": "SMblast1",
    }
    blast = {
        "_id": bid,
        "user_id": user_id,
        "sent_count": 1,
        "failed_count": 0,
        "delivered_count": 0,
        "status": "completed",
        "total_recipients": 1,
    }

    messages_coll = MagicMock()
    messages_coll.find = _empty_find()

    recipients_coll = MagicMock()
    recipients_coll.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[recipient]))
    )
    recipients_coll.update_one = AsyncMock()

    blasts_coll = MagicMock()
    blasts_coll.find_one = AsyncMock(side_effect=[blast, {**blast, "delivered_count": 1}])
    blasts_coll.update_one = AsyncMock()

    db = _db_with_messages(messages_coll, recipients_coll)
    db.blast_campaigns = blasts_coll

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
        patch("app.services.status_callback._publish_best_effort") as publish,
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SMblast1", "MessageStatus": "delivered"},
        )

    assert res.status_code == 200
    assert recipients_coll.update_one.await_count == 1
    assert blasts_coll.update_one.await_count == 1
    inc = blasts_coll.update_one.await_args[0][1]["$inc"]
    assert inc.get("delivered_count") == 1
    publish.assert_called()
    assert publish.call_args[0][1] == "blast:updated"


def test_duplicate_failure_does_not_double_count(client):
    rid = ObjectId()
    bid = ObjectId()
    recipient = {
        "_id": rid,
        "blast_id": str(bid),
        "status": "failed",
        "twilio_sid": "SMdup",
        "failed_at": utcnow(),
    }
    blast = {"_id": bid, "user_id": str(ObjectId()), "failed_count": 1, "status": "completed"}

    messages_coll = MagicMock()
    messages_coll.find = _empty_find()
    recipients_coll = MagicMock()
    recipients_coll.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[recipient]))
    )
    recipients_coll.update_one = AsyncMock()
    db = _db_with_messages(messages_coll, recipients_coll)
    db.blast_campaigns = MagicMock(
        find_one=AsyncMock(return_value=blast),
        update_one=AsyncMock(),
    )

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
        patch("app.services.status_callback._publish_best_effort"),
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SMdup", "MessageStatus": "failed"},
        )

    assert res.status_code == 200
    assert db.blast_recipients.update_one.await_count == 0
    assert db.blast_campaigns.update_one.await_count == 0


def test_redis_publish_failure_still_returns_200(client):
    msg_id = ObjectId()
    doc = {
        "_id": msg_id,
        "user_id": str(ObjectId()),
        "lead_id": str(ObjectId()),
        "status": "queued",
        "twilio_sid": "SMredis",
    }
    messages_coll = MagicMock()
    messages_coll.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[doc]))
    )
    messages_coll.update_one = AsyncMock()
    db = _db_with_messages(messages_coll)

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
        patch(
            "app.services.status_callback.get_redis",
            side_effect=RuntimeError("redis down"),
        ),
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SMredis", "MessageStatus": "delivered"},
        )

    assert res.status_code == 200
    assert messages_coll.update_one.await_count == 1


def test_tenant_isolation_updates_only_matching_sid(client):
    a_id = ObjectId()
    doc_a = {
        "_id": a_id,
        "user_id": "userA",
        "lead_id": "leadA",
        "status": "sent",
        "twilio_sid": "SMtenantA",
    }
    messages_coll = MagicMock()
    messages_coll.find = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[doc_a]))
    )
    messages_coll.update_one = AsyncMock()
    db = _db_with_messages(messages_coll)

    with (
        patch("app.routes.webhook.twilio_service.validate_signature", return_value=True),
        patch("app.services.status_callback.get_db", return_value=db),
        patch("app.services.status_callback._publish_best_effort"),
    ):
        res = client.post(
            "/api/webhook/twilio/status",
            data={"MessageSid": "SMtenantA", "MessageStatus": "delivered"},
        )

    assert res.status_code == 200
    messages_coll.find.assert_called_with({"twilio_sid": "SMtenantA"})
    update_filter = messages_coll.update_one.await_args[0][0]
    assert update_filter == {"_id": a_id}
