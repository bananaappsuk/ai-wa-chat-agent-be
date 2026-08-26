"""Meta outbound media (link-based) routing."""
from unittest.mock import MagicMock, patch

import pytest

from app.services import whatsapp_outbound as wo
from app.services.meta_whatsapp_service import MetaSendResult, media_type_for_mime


def test_media_type_for_mime():
    assert media_type_for_mime("image/jpeg") == "image"
    assert media_type_for_mime("image/webp") == "sticker"
    assert media_type_for_mime("video/mp4") == "video"
    assert media_type_for_mime("audio/ogg") == "audio"
    assert media_type_for_mime("application/pdf") == "document"
    assert media_type_for_mime(None) == "document"


def test_send_whatsapp_media_routes_to_meta():
    fake = MetaSendResult(
        provider="meta", provider_message_id="wamid.X", phone_number_id="PN", to="4479", raw={}
    )
    with patch("app.services.meta_whatsapp_service.send_media", return_value=fake) as m:
        out = wo.send_whatsapp_media(
            provider="meta",
            to="+447700900123",
            media_url="https://cdn.example.com/f.jpg",
            media_type="image",
            caption="hi",
            user={"_id": "u"},
        )
    m.assert_called_once()
    assert out["provider"] == "meta"
    assert out["provider_message_id"] == "wamid.X"
    assert out["status"] == "sent"


def test_send_whatsapp_media_rejects_twilio_via_dispatcher():
    # Twilio media stays on the native worker path, not this dispatcher.
    with pytest.raises(wo.UnknownWhatsAppProviderError):
        wo.send_whatsapp_media(
            provider="twilio", to="+447700900123", media_url="https://x/y.jpg", media_type="image"
        )


def test_meta_send_media_requires_https():
    from app.services.meta_whatsapp_service import MetaWhatsAppError, send_media

    with pytest.raises(MetaWhatsAppError):
        send_media(to="+447700900123", media_url="http://insecure/x.jpg", media_type="image", user={})


def test_worker_routes_meta_media_end_to_end():
    """Deep: send_outbound_message for a Meta message with media must resolve the public
    URL, pick the right media type, and call the Meta media sender (not text/template/twilio)."""
    from datetime import datetime, timedelta, timezone

    from bson import ObjectId

    from app.services.meta_whatsapp_service import MetaSendResult
    from app.workers import tasks as tasks_mod

    user_id, lead_id, msg_id = str(ObjectId()), ObjectId(), ObjectId()
    lead = {
        "_id": lead_id,
        "user_id": user_id,
        "phone": "+447700900001",
        "whatsapp_consent_status": "unknown",
        "blacklisted": False,
        "last_inbound_at": datetime.now(timezone.utc),
        "whatsapp_window_expires_at": datetime.now(timezone.utc) + timedelta(hours=10),
    }
    meta_msg = {
        "_id": msg_id,
        "status": "queued",
        "message_purpose": "conversational",
        "provider": "meta",
        "media_url": "/api/media/files/abc123",
        "media_content_type": "image/jpeg",
        "media_filename": "photo.jpg",
    }
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=lead)
    db.users.find_one = MagicMock(
        return_value={"_id": ObjectId(user_id), "meta_phone_number_id": "PN", "meta_connection_status": "connected"}
    )
    db.messages.update_one = MagicMock()
    db.messages.find_one = MagicMock(side_effect=[meta_msg, {"_id": msg_id, "status": "sent"}])

    fake = MetaSendResult(provider="meta", provider_message_id="wamid.Z", phone_number_id="PN", to="4479", raw={})
    with (
        patch.object(tasks_mod, "_db", return_value=db),
        patch.object(tasks_mod, "_publish"),
        patch.object(tasks_mod.settings, "PUBLIC_BASE_URL", "https://pub.example.com"),
        patch("app.services.idempotency.claim_idempotency", return_value=True),
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True),
        patch("app.services.meta_whatsapp_service.send_media", return_value=fake) as send_media_mock,
        patch.object(tasks_mod.twilio_service, "send_whatsapp") as twilio_mock,
    ):
        tasks_mod.send_outbound_message(str(msg_id), user_id, str(lead_id), None, media_url="/api/media/files/abc123")

    twilio_mock.assert_not_called()
    send_media_mock.assert_called_once()
    kwargs = send_media_mock.call_args.kwargs
    assert kwargs["media_url"] == "https://pub.example.com/api/media/files/abc123"
    assert kwargs["media_type"] == "image"
    assert kwargs["filename"] == "photo.jpg"
