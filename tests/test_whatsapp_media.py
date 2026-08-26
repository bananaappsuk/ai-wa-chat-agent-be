"""Tests for inbound/outbound WhatsApp media (B5/B6)."""
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from starlette.requests import Request

from app.services.media import parse_inbound_media, media_fields_from_items, is_allowed_mime
from app.services.whatsapp_window import WINDOW_CLOSED_ERROR


def test_inbound_image_with_empty_body():
    params = {
        "NumMedia": "1",
        "MediaUrl0": "https://api.twilio.com/media/ME1",
        "MediaContentType0": "image/jpeg",
        "Body": "",
    }
    items = parse_inbound_media(params)
    assert len(items) == 1
    assert items[0]["content_type"] == "image/jpeg"
    meta = media_fields_from_items(items, "")
    assert meta["message_type"] == "image"
    assert meta["media_url"].endswith("/ME1")
    assert meta["media_content_type"] == "image/jpeg"


def test_multiple_inbound_media_items():
    params = {
        "NumMedia": "2",
        "MediaUrl0": "https://api.twilio.com/a",
        "MediaContentType0": "image/png",
        "MediaUrl1": "https://api.twilio.com/b",
        "MediaContentType1": "application/pdf",
        "Body": "see files",
    }
    items = parse_inbound_media(params)
    assert len(items) == 2
    meta = media_fields_from_items(items, "see files")
    assert meta["message_type"] == "media"
    assert meta["media_items"][1]["content_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_unsupported_upload_rejected(tmp_path):
    from app.routes import media as media_route
    from app.services.media_storage import LocalMediaStorage

    user = {"_id": ObjectId()}
    upload = MagicMock()
    upload.filename = "x.exe"
    upload.content_type = "application/x-msdownload"
    upload.read = AsyncMock(return_value=b"MZ")

    with patch.object(media_route, "get_media_storage", return_value=LocalMediaStorage(str(tmp_path))):
        with pytest.raises(HTTPException) as exc:
            await media_route.upload_media(file=upload, user=user)
    assert exc.value.status_code == 400
    assert "Unsupported" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_oversized_upload_rejected(tmp_path):
    from app.routes import media as media_route
    from app.services.media_storage import LocalMediaStorage
    from app.config import settings

    user = {"_id": ObjectId()}
    upload = MagicMock()
    upload.filename = "big.jpg"
    upload.content_type = "image/jpeg"
    # Image cap is 5MB
    upload.read = AsyncMock(return_value=b"x" * (5 * 1024 * 1024 + 10))

    with (
        patch.object(media_route, "get_media_storage", return_value=LocalMediaStorage(str(tmp_path))),
        patch.object(settings, "MEDIA_MAX_BYTES", 16 * 1024 * 1024),
    ):
        with pytest.raises(HTTPException) as exc:
            await media_route.upload_media(file=upload, user=user)
    assert exc.value.status_code == 400
    assert "too large" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_media_proxy_tenant_protection():
    from app.routes import messages as messages_route

    owner = ObjectId()
    other = ObjectId()
    msg_id = ObjectId()
    db = MagicMock()
    db.messages.find_one = AsyncMock(return_value=None)
    db.users.find_one = AsyncMock(return_value={"_id": other})

    scope = {
        "type": "http",
        "method": "GET",
        "headers": [(b"authorization", b"Bearer faketoken")],
        "query_string": b"",
    }
    request = Request(scope)

    with (
        patch.object(messages_route, "get_db", return_value=db),
        patch.object(messages_route, "decode_token", return_value={"sub": str(other)}),
    ):
        with pytest.raises(HTTPException) as exc:
            await messages_route.proxy_message_media(str(msg_id), 0, request)
    assert exc.value.status_code == 404
    assert db.messages.find_one.await_args.args[0]["user_id"] == str(other)


@pytest.mark.asyncio
async def test_outbound_media_passed_to_twilio_and_media_only_accepted():
    from app.routes import messages as messages_route
    from app.models.message import MessageSend

    lead_id = ObjectId()
    user_id = ObjectId()
    lead = {
        "_id": lead_id,
        "phone": "+447700900000",
        "blacklisted": False,
        "last_inbound_at": datetime.now(timezone.utc) - timedelta(hours=1),
        "whatsapp_window_expires_at": datetime.now(timezone.utc) + timedelta(hours=20),
    }
    msg_doc = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "lead_id": str(lead_id),
        "direction": "outbound",
        "message": "[image]",
        "status": "queued",
        "media_url": "https://example.com/x.jpg",
        "created_at": datetime.now(timezone.utc),
    }

    with (
        patch.object(messages_route.lead_service, "get_lead", new=AsyncMock(return_value=lead)),
        patch.object(
            messages_route.message_service,
            "insert_message",
            new=AsyncMock(return_value=msg_doc),
        ) as insert_mock,
        patch.object(messages_route.ws_manager, "push", new=AsyncMock()),
        patch.object(messages_route, "enqueue") as enqueue_mock,
        patch("app.security.rate_limit.rate_limit_send"),
        patch("app.services.whatsapp_eligibility._sender_ok", return_value=True),
        patch.object(
            messages_route,
            "resolve_lead_whatsapp_provider",
            new=AsyncMock(return_value="twilio"),
        ),
        patch.object(
            messages_route,
            "get_db",
            return_value=MagicMock(
                messages=MagicMock(
                    update_one=AsyncMock(),
                    find_one=AsyncMock(return_value=msg_doc),
                )
            ),
        ),
    ):
        result = await messages_route.send_message(
            MessageSend(
                lead_id=str(lead_id),
                media_url="https://example.com/x.jpg",
                media_content_type="image/jpeg",
                media_filename="x.jpg",
            ),
            user={"_id": user_id, "plan": "business", "subscription_status": "active"},
        )

    assert result["status"] == "queued"
    assert insert_mock.await_args.kwargs["media_url"] == "https://example.com/x.jpg"
    assert enqueue_mock.call_args.kwargs.get("media_url") == "https://example.com/x.jpg" or (
        enqueue_mock.call_args.args and "https://example.com/x.jpg" in str(enqueue_mock.call_args)
    )


@pytest.mark.asyncio
async def test_outside_window_media_blocked():
    from app.routes import messages as messages_route
    from app.models.message import MessageSend

    lead = {
        "_id": ObjectId(),
        "phone": "+447700900000",
        "blacklisted": False,
        "last_inbound_at": None,
        "whatsapp_window_expires_at": None,
    }
    with patch.object(messages_route.lead_service, "get_lead", new=AsyncMock(return_value=lead)):
        with patch("app.security.rate_limit.rate_limit_send"):
            with patch("app.services.whatsapp_eligibility._sender_ok", return_value=True):
                with patch(
                    "app.routes.messages.resolve_lead_whatsapp_provider",
                    new=AsyncMock(return_value="twilio"),
                ):
                    with pytest.raises(HTTPException) as exc:
                        await messages_route.send_message(
                            MessageSend(
                                lead_id=str(lead["_id"]),
                                media_url="https://example.com/x.jpg",
                                media_content_type="image/jpeg",
                            ),
                            user={"_id": ObjectId()},
                        )
    assert exc.value.status_code == 400
    assert WINDOW_CLOSED_ERROR in str(exc.value.detail)


def test_allowed_mime_list():
    assert is_allowed_mime("image/jpeg")
    assert is_allowed_mime("application/pdf")
    assert not is_allowed_mime("application/x-msdownload")


def test_worker_passes_media_url_to_twilio():
    from app.workers import tasks as tasks_mod

    user_id = str(ObjectId())
    lead_id = ObjectId()
    msg_id = ObjectId()
    lead = {
        "_id": lead_id,
        "user_id": user_id,
        "phone": "+447700900001",
        "whatsapp_consent_status": "unknown",
        "blacklisted": False,
        "last_inbound_at": datetime.now(timezone.utc),
        "whatsapp_window_expires_at": datetime.now(timezone.utc) + timedelta(hours=10),
    }
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=lead)
    db.messages.update_one = MagicMock()
    db.messages.find_one = MagicMock(
        side_effect=[
            {"_id": msg_id, "status": "queued", "message_purpose": "conversational"},
            {"_id": msg_id, "status": "sent"},
        ]
    )

    with (
        patch.object(tasks_mod, "_db", return_value=db),
        patch.object(tasks_mod.twilio_service, "send_whatsapp", return_value={"sid": "SMx", "status": "queued"}) as send_mock,
        patch.object(tasks_mod, "_publish"),
        patch.object(tasks_mod.settings, "PUBLIC_BASE_URL", "https://pub.example.com"),
        patch("app.services.idempotency.claim_idempotency", return_value=True),
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch("app.services.whatsapp_eligibility._sender_ok", return_value=True),
    ):
        tasks_mod.send_outbound_message(
            str(msg_id),
            user_id,
            str(lead_id),
            None,
            media_url="/api/media/files/abc123",
        )

    send_mock.assert_called_once()
    kwargs = send_mock.call_args.kwargs
    assert kwargs["media_url"] == "https://pub.example.com/api/media/files/abc123"
    assert kwargs.get("body") in (None, "")
