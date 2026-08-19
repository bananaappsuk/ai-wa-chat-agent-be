"""Meta Cloud API inbound media download (Graph metadata + binary).

Never fetches URLs from the webhook body. Token is never logged or persisted.
"""
from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx

from app.config import settings
from app.security.mime_sniff import resolve_upload_mime
from app.security.ssrf import assert_safe_meta_media_url
from app.services.media import is_allowed_mime, max_bytes_for_mime, media_fields_from_items, normalize_mime
from app.services.media_storage import get_media_storage, safe_filename

logger = logging.getLogger(__name__)

_MAX_REDIRECTS = 3
_EXT_FOR_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/aac": ".aac",
    "audio/amr": ".amr",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "text/plain": ".txt",
}


class MetaMediaError(Exception):
    """Inbound media fetch/validation/storage failed (webhook should still persist)."""


def _auth_headers(access_token: str) -> dict[str, str]:
    token = (access_token or "").strip()
    if not token:
        raise MetaMediaError("Meta credentials are not configured")
    return {"Authorization": f"Bearer {token}"}


def _timeout() -> float:
    return max(5.0, float(settings.META_HTTP_TIMEOUT_SECONDS or 30.0))


def _graph_version() -> str:
    return (settings.META_GRAPH_VERSION or "v21.0").strip().lstrip("/")


def fallback_filename(*, kind: str | None, mime: str | None, filename: str | None) -> str:
    if filename and filename.strip():
        return safe_filename(filename.strip())
    ext = _EXT_FOR_MIME.get(normalize_mime(mime), "")
    base = (kind or "file").strip().lower() or "file"
    if base not in ("image", "audio", "video", "document", "sticker", "voice"):
        base = "file"
    if base == "sticker":
        base = "image"
    if base == "voice":
        base = "audio"
    return safe_filename(f"{base}{ext or '.bin'}")


def _absolute_redirect(current: str, location: str) -> str:
    loc = (location or "").strip()
    if not loc:
        raise MetaMediaError("Empty redirect")
    return urljoin(current, loc)


async def fetch_graph_media_metadata(
    media_id: str,
    *,
    client: httpx.AsyncClient,
    access_token: str,
) -> dict[str, Any]:
    mid = (media_id or "").strip()
    if not mid or "/" in mid or "?" in mid or ".." in mid:
        raise MetaMediaError("Invalid media id")
    url = f"https://graph.facebook.com/{_graph_version()}/{mid}"
    resp = await client.get(url, headers=_auth_headers(access_token))
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.is_error or not isinstance(data, dict):
        raise MetaMediaError("Meta media metadata request failed")
    tmp = str(data.get("url") or "").strip()
    if not tmp:
        raise MetaMediaError("Meta media metadata missing url")
    return {
        "url": tmp,
        "mime_type": str(data.get("mime_type") or "").strip() or None,
        "file_size": data.get("file_size"),
        "sha256": str(data.get("sha256") or "").strip() or None,
        "id": str(data.get("id") or mid),
    }


async def _download_bytes(
    start_url: str,
    *,
    client: httpx.AsyncClient,
    max_bytes: int,
    access_token: str,
) -> bytes:
    try:
        url = assert_safe_meta_media_url(start_url)
    except ValueError as exc:
        raise MetaMediaError(str(exc)) from exc
    hops = 0
    while True:
        req = client.build_request("GET", url, headers=_auth_headers(access_token))
        resp = await client.send(req, follow_redirects=False)
        if resp.status_code in (301, 302, 303, 307, 308):
            hops += 1
            if hops > _MAX_REDIRECTS:
                await resp.aclose()
                raise MetaMediaError("Too many media redirects")
            loc = resp.headers.get("location") or ""
            await resp.aclose()
            nxt = _absolute_redirect(url, loc)
            try:
                url = assert_safe_meta_media_url(nxt)
            except ValueError as exc:
                raise MetaMediaError(str(exc)) from exc
            continue
        if resp.is_error:
            await resp.aclose()
            raise MetaMediaError("Meta media download failed")
        cl = resp.headers.get("content-length")
        if cl:
            try:
                if int(cl) > max_bytes:
                    await resp.aclose()
                    raise MetaMediaError("Meta media exceeds size limit")
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                await resp.aclose()
                raise MetaMediaError("Meta media exceeds size limit")
            chunks.append(chunk)
        await resp.aclose()
        return b"".join(chunks)


async def download_and_store_meta_media(
    *,
    media_id: str,
    user_id: str,
    user: Optional[dict] = None,
    filename_hint: str | None = None,
    declared_mime: str | None = None,
    kind: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Return media_fields_from_items dict with internal /api/media/files URL."""
    from app.services.meta_credentials import MetaCredentialsError, get_meta_credentials_for_user

    try:
        creds = get_meta_credentials_for_user(user)
    except MetaCredentialsError as exc:
        raise MetaMediaError(str(exc)) from exc
    token = creds.access_token
    own = client is None
    timeout = httpx.Timeout(_timeout())
    http = client or httpx.AsyncClient(timeout=timeout, follow_redirects=False)
    try:
        meta = await fetch_graph_media_metadata(media_id, client=http, access_token=token)
        try:
            size_hint = int(meta.get("file_size") or 0)
        except (TypeError, ValueError):
            size_hint = 0
        declared = normalize_mime(meta.get("mime_type") or declared_mime)
        cap = min(int(settings.MEDIA_MAX_BYTES), max_bytes_for_mime(declared or "application/octet-stream"))
        if size_hint and size_hint > cap:
            raise MetaMediaError("Meta media exceeds size limit")
        raw = await _download_bytes(meta["url"], client=http, max_bytes=cap, access_token=token)
        name = fallback_filename(kind=kind, mime=declared, filename=filename_hint)
        resolved = resolve_upload_mime(raw, declared, name)
        if not resolved or not is_allowed_mime(resolved):
            raise MetaMediaError("Unsupported or untrusted media type")
        stored = get_media_storage().save(
            user_id=user_id,
            filename=name,
            content_type=resolved,
            data=raw,
        )
        item = {
            "url": stored.get("path") or f"/api/media/files/{stored['id']}",
            "content_type": resolved,
            "filename": stored.get("filename") or name,
            "index": 0,
        }
        fields = media_fields_from_items([item], "")
        return fields
    except MetaMediaError:
        raise
    except httpx.HTTPError as exc:
        raise MetaMediaError("Meta media HTTP error") from exc
    except ValueError as exc:
        raise MetaMediaError(str(exc)) from exc
    finally:
        if own:
            await http.aclose()


def media_pnid_allows_download(*, user: dict, webhook_pnid: str | None) -> bool:
    """Webhook PNID must match the tenant binding. No env PNID equality for connected tenants."""
    user_pnid = str((user or {}).get("meta_phone_number_id") or "").strip()
    hook = (webhook_pnid or "").strip()
    if not user_pnid or not hook:
        return False
    if user_pnid != hook:
        return False
    status = str((user or {}).get("meta_connection_status") or "").strip().lower()
    if status == "connected":
        return True
    from app.services.meta_credentials import legacy_poc_fallback_allowed

    if status == "legacy_poc" and legacy_poc_fallback_allowed(user):
        return True
    if status == "legacy_poc":
        # Migrated legacy_poc with encrypted creds still owns this PNID.
        return True
    return False
