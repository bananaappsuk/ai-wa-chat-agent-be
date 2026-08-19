"""Phase 2E Meta inbound media (mocked Graph HTTP; no live Meta)."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import httpx
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from app.main import app
from app.services import meta_whatsapp_service
from app.services.inbound_whatsapp import InboundMessage, process_inbound_message
from app.services.meta_media import (
    MetaMediaError,
    download_and_store_meta_media,
    fallback_filename,
    fetch_graph_media_metadata,
)
from app.services.meta_whatsapp_service import parse_inbound_messages
from app.services.meta_credentials import MetaTenantCredentials
from app.workers import tasks
from tests.test_meta_inbound_phase2a import (
    MemDB,
    _post_meta,
    _user,
    patched_meta,
)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 120
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
OGG = b"OggS" + b"\x00" * 80
TOKEN = "secret-token-NEVER-STORE"
_CRED = MetaTenantCredentials(access_token=TOKEN, phone_number_id="PN_A", waba_id="WABA")
GRAPH_TMP = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=MEDIA99"
BIN_HOST_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=MEDIA99"


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pushes():
    items: list[tuple] = []

    async def _push(user_id, event, data):
        items.append((str(user_id), event, data))

    return items, _push


@pytest.fixture
def mem():
    return MemDB()


def _media_payload(
    *,
    phone_number_id: str,
    wamid: str,
    msg_type: str,
    media: dict,
    from_wa: str = "447700900123",
    webhook_url: str | None = None,
) -> dict:
    block = dict(media)
    if webhook_url:
        block["url"] = webhook_url
    key = "audio" if msg_type in ("audio", "voice") else msg_type
    if msg_type == "voice" and "id" in block:
        key = "audio"
    msg = {
        "from": from_wa,
        "id": wamid,
        "timestamp": "1710000000",
        "type": msg_type,
        key: block,
    }
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
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"profile": {"name": "Test"}, "wa_id": from_wa}],
                            "messages": [msg],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _public_addrinfo(host, port, *args, **kwargs):
    return [(0, 0, 0, "", ("1.2.3.4", port or 443))]


class FakeStorage:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def save(self, **kwargs):
        if kwargs.get("fail"):
            raise RuntimeError("disk full")
        self.calls.append(kwargs)
        fid = "storedfile1"
        path = f"/api/media/files/{fid}"
        return {
            "id": fid,
            "filename": kwargs.get("filename") or "file",
            "content_type": kwargs.get("content_type"),
            "path": path,
            "url": path,
        }


def _handler_factory(
    *,
    graph_status=200,
    graph_json=None,
    bin_status=200,
    bin_body=JPEG,
    bin_headers=None,
    redirect_to=None,
    seen=None,
):
    seen = seen if seen is not None else []
    graph_json = graph_json if graph_json is not None else {
        "url": GRAPH_TMP,
        "mime_type": "image/jpeg",
        "file_size": len(JPEG),
        "sha256": "abc",
        "id": "MEDIA99",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        host = urlparse(str(request.url)).hostname or ""
        if host == "graph.facebook.com":
            return httpx.Response(graph_status, json=graph_json if graph_status == 200 else {"error": {"message": "no"}})
        if redirect_to and request.url.path.endswith("first"):
            return httpx.Response(302, headers={"location": redirect_to})
        hdrs = {"content-type": "application/octet-stream"}
        if bin_headers:
            hdrs.update(bin_headers)
        return httpx.Response(bin_status, content=bin_body if bin_status == 200 else b"", headers=hdrs)

    return handler, seen


def _patch_async_client(handler):
    class _C(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            kwargs["follow_redirects"] = False
            super().__init__(*args, **kwargs)

    return patch("app.services.meta_media.httpx.AsyncClient", _C)


def test_parse_image_media_id():
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.IMG1",
        msg_type="image",
        media={"id": "12345", "mime_type": "image/jpeg"},
    )
    msg = parse_inbound_messages(payload)[0]
    assert msg.media_id == "12345"
    assert msg.kind == "image"
    assert msg.text == ""
    assert "[image]" not in (msg.text or "")


def test_parse_image_caption():
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.IMG2",
        msg_type="image",
        media={"id": "12345", "mime_type": "image/jpeg", "caption": "beach"},
    )
    msg = parse_inbound_messages(payload)[0]
    assert msg.text == "beach"
    assert msg.media_id == "12345"


def test_parse_document_fields():
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.DOC1",
        msg_type="document",
        media={
            "id": "d1",
            "mime_type": "application/pdf",
            "filename": "Quote.pdf",
            "caption": "see quote",
        },
    )
    msg = parse_inbound_messages(payload)[0]
    assert msg.media_id == "d1"
    assert msg.mime_type == "application/pdf"
    assert msg.filename == "Quote.pdf"
    assert msg.text == "see quote"
    assert msg.kind == "document"


def test_parse_audio():
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.AUD1",
        msg_type="audio",
        media={"id": "a1", "mime_type": "audio/ogg"},
    )
    msg = parse_inbound_messages(payload)[0]
    assert msg.media_id == "a1"
    assert msg.kind == "audio"
    assert msg.voice is False


def test_parse_video():
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.VID1",
        msg_type="video",
        media={"id": "v1", "mime_type": "video/mp4"},
    )
    msg = parse_inbound_messages(payload)[0]
    assert msg.kind == "video"
    assert msg.media_id == "v1"


def test_parse_voice_mapped():
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.VO1",
        msg_type="voice",
        media={"id": "vo1", "mime_type": "audio/ogg", "voice": True},
    )
    msg = parse_inbound_messages(payload)[0]
    assert msg.voice is True
    assert msg.kind == "audio"
    assert msg.media_id == "vo1"


def test_parse_sticker():
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.ST1",
        msg_type="sticker",
        media={"id": "st1", "mime_type": "image/webp"},
    )
    msg = parse_inbound_messages(payload)[0]
    assert msg.kind == "image"
    assert msg.media_id == "st1"
    assert msg.text == ""


@pytest.mark.asyncio
async def test_metadata_and_binary_use_bearer_auth():
    handler, seen = _handler_factory()
    store = FakeStorage()
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=_CRED),
        patch("app.services.meta_media.settings.META_GRAPH_VERSION", "v21.0"),
        patch("app.services.meta_media.get_media_storage", return_value=store),
        patch("app.security.ssrf.socket.getaddrinfo", _public_addrinfo),
        _patch_async_client(handler),
    ):
        fields = await download_and_store_meta_media(
            media_id="MEDIA99",
            user_id="tenant1",
            user=_user("PN_A"),
            filename_hint="pic.jpg",
            declared_mime="image/jpeg",
            kind="image",
        )
    assert len(seen) >= 2
    for req in seen:
        assert req.headers.get("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(fields)
    assert fields["media_url"].startswith("/api/media/files/")
    assert GRAPH_TMP not in json.dumps(fields)
    assert store.calls[0]["user_id"] == "tenant1"


@pytest.mark.asyncio
async def test_webhook_url_cannot_control_download_destination():
    evil = "https://evil.example/steal"
    seen: list[httpx.Request] = []
    handler, seen = _handler_factory(seen=seen)

    store = FakeStorage()
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=_CRED),
        patch("app.services.meta_media.get_media_storage", return_value=store),
        patch("app.security.ssrf.socket.getaddrinfo", _public_addrinfo),
        _patch_async_client(handler),
    ):
        await download_and_store_meta_media(
            media_id="MEDIA99", user_id="t1", user=_user("PN_A"), kind="image"
        )
    hosts = [(urlparse(str(r.url)).hostname or "") for r in seen]
    assert "evil.example" not in hosts
    assert any(h == "graph.facebook.com" for h in hosts)
    assert any(h and h.endswith("fbsbx.com") for h in hosts)


@pytest.mark.asyncio
async def test_non_allowlisted_host_rejected():
    handler, _ = _handler_factory(
        graph_json={"url": "https://evil.example/x", "mime_type": "image/jpeg", "file_size": 10}
    )
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=_CRED),
        patch("app.security.ssrf.socket.getaddrinfo", _public_addrinfo),
        _patch_async_client(handler),
        pytest.raises(MetaMediaError),
    ):
        await download_and_store_meta_media(
            media_id="MEDIA99", user_id="t1", user=_user("PN_A"), kind="image"
        )


@pytest.mark.asyncio
async def test_redirect_to_non_allowlisted_host_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        host = urlparse(str(request.url)).hostname or ""
        if host == "graph.facebook.com":
            return httpx.Response(
                200,
                json={
                    "url": "https://lookaside.fbsbx.com/first",
                    "mime_type": "image/jpeg",
                    "file_size": 10,
                },
            )
        if str(request.url).endswith("/first") or request.url.path.endswith("/first"):
            return httpx.Response(302, headers={"location": "https://evil.example/x"})
        return httpx.Response(200, content=JPEG)

    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=_CRED),
        patch("app.security.ssrf.socket.getaddrinfo", _public_addrinfo),
        _patch_async_client(handler),
        pytest.raises(MetaMediaError),
    ):
        await download_and_store_meta_media(
            media_id="MEDIA99", user_id="t1", user=_user("PN_A"), kind="image"
        )


@pytest.mark.asyncio
async def test_https_enforced():
    handler, _ = _handler_factory(
        graph_json={"url": "http://lookaside.fbsbx.com/x", "mime_type": "image/jpeg", "file_size": 10}
    )
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=_CRED),
        patch("app.security.ssrf.socket.getaddrinfo", _public_addrinfo),
        _patch_async_client(handler),
        pytest.raises(MetaMediaError),
    ):
        await download_and_store_meta_media(
            media_id="MEDIA99", user_id="t1", user=_user("PN_A"), kind="image"
        )


@pytest.mark.asyncio
async def test_size_limit_enforced():
    handler, _ = _handler_factory(
        graph_json={"url": GRAPH_TMP, "mime_type": "image/jpeg", "file_size": 99_000_000}
    )
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=_CRED),
        patch("app.services.meta_media.settings.MEDIA_MAX_BYTES", 1024),
        patch("app.security.ssrf.socket.getaddrinfo", _public_addrinfo),
        _patch_async_client(handler),
        pytest.raises(MetaMediaError),
    ):
        await download_and_store_meta_media(
            media_id="MEDIA99", user_id="t1", user=_user("PN_A"), kind="image"
        )


@pytest.mark.asyncio
async def test_mime_validation_enforced():
    handler, _ = _handler_factory(
        bin_body=b"\x00\x01\x02\x03" + b"\xff" * 40,
        graph_json={
            "url": GRAPH_TMP,
            "mime_type": "application/octet-stream",
            "file_size": 44,
        },
    )
    store = FakeStorage()
    with (
        patch("app.services.meta_credentials.get_meta_credentials_for_user", return_value=_CRED),
        patch("app.services.meta_media.get_media_storage", return_value=store),
        patch("app.security.ssrf.socket.getaddrinfo", _public_addrinfo),
        _patch_async_client(handler),
        pytest.raises(MetaMediaError),
    ):
        await download_and_store_meta_media(
            media_id="MEDIA99",
            user_id="t1",
            user=_user("PN_A"),
            declared_mime="image/jpeg",
            kind="image",
        )
    assert store.calls == []


def test_safe_filename():
    name = fallback_filename(
        kind="document",
        mime="application/pdf",
        filename="../../etc/passwd.pdf",
    )
    assert ".." not in name
    assert name.endswith(".pdf") or "passwd" in name
    assert "/" not in name
    assert "\\" not in name
    empty = fallback_filename(kind="image", mime="image/jpeg", filename=None)
    assert empty.startswith("image")


@pytest.mark.asyncio
async def test_fetch_graph_metadata_bearer():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"url": GRAPH_TMP, "mime_type": "image/jpeg", "file_size": 1, "sha256": "x"},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with patch("app.services.meta_media.settings.META_ACCESS_TOKEN", TOKEN):
            data = await fetch_graph_media_metadata("MEDIA99", client=client, access_token=TOKEN)
    assert seen[0].headers.get("Authorization") == f"Bearer {TOKEN}"
    assert data["url"] == GRAPH_TMP


def test_persist_internal_url_and_meta_fields(client, mem, pushes):
    items, push = pushes
    user = _user("PN_A")
    mem.users.docs.append(user)
    fields = {
        "message_type": "image",
        "media_items": [
            {"url": "/api/media/files/abc", "content_type": "image/jpeg", "filename": "image.jpg", "index": 0}
        ],
        "media_url": "/api/media/files/abc",
        "media_content_type": "image/jpeg",
        "media_filename": "image.jpg",
    }
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.P1",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg"},
        webhook_url="https://evil.example/from-webhook",
    )
    dl = AsyncMock(return_value=fields)
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", dl
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        res = _post_meta(client, payload)
    assert res.status_code == 200
    msg = mem.messages.docs[0]
    assert msg["provider"] == "meta"
    assert msg["provider_message_id"] == "wamid.P1"
    assert "twilio_sid" not in msg
    assert msg["media_url"] == "/api/media/files/abc"
    assert GRAPH_TMP not in json.dumps(msg, default=str)
    assert "evil.example" not in json.dumps(msg, default=str)
    assert TOKEN not in json.dumps(msg, default=str)
    assert dl.await_count == 1
    dumped = json.dumps(msg, default=str)
    assert "graph.facebook.com" not in dumped or "/api/media/files" in dumped
    assert msg["message"] == "[image]"
    assert len([e for _, e, _ in items if e == "message:new"]) == 1


def test_caption_stored_as_message(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    fields = {
        "message_type": "image",
        "media_items": [{"url": "/api/media/files/x", "content_type": "image/jpeg", "filename": "a.jpg", "index": 0}],
        "media_url": "/api/media/files/x",
        "media_content_type": "image/jpeg",
        "media_filename": "a.jpg",
    }
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.CAP1",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg", "caption": "hello pic"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", AsyncMock(return_value=fields)
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        _post_meta(client, payload)
    assert mem.messages.docs[0]["message"] == "hello pic"


def test_pure_media_does_not_enqueue_ai(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    fields = {
        "message_type": "image",
        "media_items": [{"url": "/api/media/files/x", "content_type": "image/jpeg", "filename": "a.jpg", "index": 0}],
        "media_url": "/api/media/files/x",
        "media_content_type": "image/jpeg",
        "media_filename": "a.jpg",
    }
    enq = MagicMock()
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.NOAI",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg"},
    )
    with patched_meta(mem, push, enqueue=enq), patch(
        "app.services.meta_media.download_and_store_meta_media", AsyncMock(return_value=fields)
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"), patch(
        "app.config.settings.OPENAI_API_KEY", "sk-test"
    ), patch("app.config.settings.AI_FEATURES_ENABLED", True):
        _post_meta(client, payload)
    ai_calls = [c for c in enq.call_args_list if c.args and c.args[0] is tasks.generate_and_send_ai_reply]
    assert ai_calls == []
    classify = [c for c in enq.call_args_list if c.args and "classify" in str(c.args[0])]
    assert classify == []


def test_caption_may_enqueue_ai(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    fields = {
        "message_type": "image",
        "media_items": [{"url": "/api/media/files/x", "content_type": "image/jpeg", "filename": "a.jpg", "index": 0}],
        "media_url": "/api/media/files/x",
        "media_content_type": "image/jpeg",
        "media_filename": "a.jpg",
    }
    enq = MagicMock()
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.AIIMG",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg", "caption": "what is this?"},
    )
    with patched_meta(mem, push, enqueue=enq), patch(
        "app.services.meta_media.download_and_store_meta_media", AsyncMock(return_value=fields)
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"), patch(
        "app.config.settings.OPENAI_API_KEY", "sk-test"
    ), patch("app.config.settings.AI_FEATURES_ENABLED", True):
        _post_meta(client, payload)
    ai_calls = [c for c in enq.call_args_list if c.args and c.args[0] is tasks.generate_and_send_ai_reply]
    assert len(ai_calls) == 1


def test_pure_media_does_not_trigger_stop(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    fields = {
        "message_type": "image",
        "media_items": [{"url": "/api/media/files/x", "content_type": "image/jpeg", "filename": "STOP.jpg", "index": 0}],
        "media_url": "/api/media/files/x",
        "media_content_type": "image/jpeg",
        "media_filename": "STOP.jpg",
    }
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.NOSTOP",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", AsyncMock(return_value=fields)
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        _post_meta(client, payload)
    lead = mem.leads.docs[0]
    assert lead.get("whatsapp_consent_status") != "opted_out"
    assert not mem.blacklist.docs


def test_document_filename_stop_does_not_opt_out(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    fields = {
        "message_type": "document",
        "media_items": [
            {"url": "/api/media/files/x", "content_type": "application/pdf", "filename": "STOP.pdf", "index": 0}
        ],
        "media_url": "/api/media/files/x",
        "media_content_type": "application/pdf",
        "media_filename": "STOP.pdf",
    }
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.STOPPDF",
        msg_type="document",
        media={"id": "MEDIA99", "mime_type": "application/pdf", "filename": "STOP.pdf"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", AsyncMock(return_value=fields)
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        _post_meta(client, payload)
    assert mem.leads.docs[0].get("whatsapp_consent_status") != "opted_out"


def test_stop_caption_opts_out(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    fields = {
        "message_type": "image",
        "media_items": [{"url": "/api/media/files/x", "content_type": "image/jpeg", "filename": "a.jpg", "index": 0}],
        "media_url": "/api/media/files/x",
        "media_content_type": "image/jpeg",
        "media_filename": "a.jpg",
    }
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.STOPCAP",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg", "caption": "STOP"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", AsyncMock(return_value=fields)
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        _post_meta(client, payload)
    assert mem.leads.docs[0]["whatsapp_consent_status"] == "opted_out"


def test_duplicate_wamid_no_redownload_or_reinsert(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    fields = {
        "message_type": "image",
        "media_items": [{"url": "/api/media/files/x", "content_type": "image/jpeg", "filename": "a.jpg", "index": 0}],
        "media_url": "/api/media/files/x",
        "media_content_type": "image/jpeg",
        "media_filename": "a.jpg",
    }
    dl = AsyncMock(return_value=fields)
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.DUPM",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", dl
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        assert _post_meta(client, payload).status_code == 200
        assert _post_meta(client, payload).status_code == 200
    assert dl.await_count == 1
    assert len(mem.messages.docs) == 1
    assert len([e for _, e, _ in items if e == "message:new"]) == 1


def test_failed_metadata_still_persists(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.FAILMD",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media",
        AsyncMock(side_effect=MetaMediaError("metadata")),
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        assert _post_meta(client, payload).status_code == 200
    msg = mem.messages.docs[0]
    assert msg["message"] == "[image]"
    assert not msg.get("media_url")
    assert not msg.get("media_items")
    assert msg["message_type"] in ("image", "sticker")


def test_failed_download_still_persists(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.FAILDL",
        msg_type="audio",
        media={"id": "MEDIA99", "mime_type": "audio/ogg"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media",
        AsyncMock(side_effect=MetaMediaError("download")),
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        assert _post_meta(client, payload).status_code == 200
    msg = mem.messages.docs[0]
    assert msg["message"] == "[audio]"
    assert not msg.get("media_url")


def test_storage_failure_still_persists(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.FAILST",
        msg_type="video",
        media={"id": "MEDIA99", "mime_type": "video/mp4"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media",
        AsyncMock(side_effect=RuntimeError("disk")),
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"):
        assert _post_meta(client, payload).status_code == 200
    msg = mem.messages.docs[0]
    assert msg["message"] == "[video]"
    assert not msg.get("media_url")


def test_unknown_tenant_does_not_download(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_OTHER"))
    dl = AsyncMock(return_value={})
    payload = _media_payload(
        phone_number_id="PN_UNKNOWN",
        wamid="wamid.UNK",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", dl
    ):
        assert _post_meta(client, payload).status_code == 200
    assert dl.await_count == 0
    assert mem.messages.docs == []


def test_env_pnid_mismatch_still_downloads_when_tenant_matches_webhook(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    dl = AsyncMock(return_value={
        "message_type": "image",
        "media_items": [{"url": "/api/media/files/abc", "content_type": "image/jpeg", "filename": "image.jpg", "index": 0}],
        "media_url": "/api/media/files/abc",
        "media_content_type": "image/jpeg",
        "media_filename": "image.jpg",
    })
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.MM",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", dl
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_ENV"):
        assert _post_meta(client, payload).status_code == 200
    assert dl.await_count == 1


@pytest.mark.asyncio
async def test_twilio_media_unaffected(mem, pushes):
    items, push = pushes
    uid = ObjectId()
    mem.users.docs.append({"_id": uid, "twilio_whatsapp_to": "whatsapp:+15550001111"})
    dl = AsyncMock(side_effect=AssertionError("Meta download must not run for Twilio"))
    inbound = InboundMessage(
        provider="twilio",
        provider_message_id="SM123",
        customer_phone="+447700900123",
        business_identifier="whatsapp:+15550001111",
        body="",
        profile_name=None,
        timestamp=None,
        message_type="image",
        skip_provider_outbound=True,
        skip_ai_jobs=True,
        media_meta={
            "message_type": "image",
            "media_url": "https://api.twilio.com/media/ME1",
            "media_content_type": "image/jpeg",
            "media_filename": None,
            "media_items": [
                {
                    "url": "https://api.twilio.com/media/ME1",
                    "content_type": "image/jpeg",
                    "filename": None,
                    "index": 0,
                }
            ],
        },
        media_id="should-ignore",
    )
    with patch("app.services.inbound_whatsapp.ws_manager.push", new=push), patch(
        "app.services.lead_service.get_db", return_value=mem
    ), patch("app.services.lead_scoring.get_db", return_value=mem), patch(
        "app.services.meta_media.download_and_store_meta_media", dl
    ):
        result = await process_inbound_message(inbound, db=mem)
    assert result.outcome == "ok"
    assert dl.await_count == 0
    msg = mem.messages.docs[0]
    assert msg["provider"] == "twilio"
    assert msg["twilio_sid"] == "SM123"
    assert msg["media_url"] == "https://api.twilio.com/media/ME1"


def test_token_never_in_persisted_message(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    fields = {
        "message_type": "image",
        "media_items": [{"url": "/api/media/files/x", "content_type": "image/jpeg", "filename": "a.jpg", "index": 0}],
        "media_url": "/api/media/files/x",
        "media_content_type": "image/jpeg",
        "media_filename": "a.jpg",
    }
    payload = _media_payload(
        phone_number_id="PN_A",
        wamid="wamid.TOK",
        msg_type="image",
        media={"id": "MEDIA99", "mime_type": "image/jpeg"},
    )
    with patched_meta(mem, push), patch(
        "app.services.meta_media.download_and_store_meta_media", AsyncMock(return_value=fields)
    ), patch("app.config.settings.META_PHONE_NUMBER_ID", "PN_A"), patch(
        "app.config.settings.META_ACCESS_TOKEN", TOKEN
    ):
        _post_meta(client, payload)
    blob = json.dumps(mem.messages.docs[0], default=str)
    assert TOKEN not in blob
