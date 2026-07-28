"""Media upload + opaque file serving (Twilio-fetchable UUID URLs)."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.middleware.auth import current_user
from app.security.mime_sniff import mime_from_filename, resolve_upload_mime
from app.security.rate_limit import rate_limit_upload
from app.services.media import (
    is_allowed_mime,
    max_bytes_for_mime,
    message_type_for_mime,
    normalize_mime,
)
from app.services.media_storage import get_media_storage, safe_filename

router = APIRouter(prefix="/media", tags=["media"])

_BLOCKED_EXT = re.compile(
    r"\.(html?|svg|js|mjs|cjs|php|exe|bat|cmd|sh|ps1|dll|jar|wasm)(\.|$)",
    re.I,
)
_FILE_ID = re.compile(r"^[a-f0-9]{16,64}$")


@router.post("/upload")
async def upload_media(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> dict:
    user_id = str(user["_id"])
    rate_limit_upload(user_id)

    original_name = file.filename or "upload"
    if _BLOCKED_EXT.search(original_name):
        raise HTTPException(status_code=400, detail="Unsupported or unsafe filename")
    filename = safe_filename(original_name)
    if _BLOCKED_EXT.search(filename):
        raise HTTPException(status_code=400, detail="Unsupported or unsafe filename")

    declared = normalize_mime(file.content_type)
    # Browsers (especially Windows) often send empty/octet-stream — use extension hint early
    # for size-limit selection; final type is resolved after reading bytes.
    provisional = declared
    if provisional in ("", "application/octet-stream", "binary/octet-stream"):
        provisional = mime_from_filename(original_name) or provisional
    if provisional and not is_allowed_mime(provisional) and declared and is_allowed_mime(declared):
        provisional = declared
    if provisional and not is_allowed_mime(provisional):
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type: {provisional or declared or 'unknown'}",
        )

    limit = min(settings.MEDIA_MAX_BYTES, max_bytes_for_mime(provisional or "application/octet-stream"))
    raw = await file.read(limit + 1)
    if len(raw) > limit:
        raise HTTPException(
            status_code=400,
            detail=f"File too large. Max {limit // (1024 * 1024)}MB for this type.",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")

    content_type = resolve_upload_mime(raw, declared, original_name)
    if not content_type or not is_allowed_mime(content_type):
        raise HTTPException(
            status_code=400,
            detail="File content does not match declared type",
        )

    stored = get_media_storage().save(
        user_id=user_id,
        filename=filename,
        content_type=content_type,
        data=raw,
    )
    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    url = f"{base}{stored['path']}" if base else stored["url"]
    return {
        "id": stored["id"],
        "url": url,
        "path": stored["path"],
        "content_type": content_type,
        "filename": filename,
        "size": stored["size"],
        "message_type": message_type_for_mime(content_type),
    }


@router.get("/files/{file_id}")
async def serve_media_file(file_id: str):
    """Opaque UUID URL for Twilio (and browsers). Security = unguessable ID + path jail."""
    if not file_id or not _FILE_ID.match(file_id):
        raise HTTPException(status_code=404, detail="Not found")
    storage = get_media_storage()
    try:
        content_type, filename, path = storage.resolve_path(file_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found") from None

    # Ensure resolved path stays inside storage root
    root = storage.root.resolve()  # type: ignore[attr-defined]
    resolved = path.resolve()
    if root not in resolved.parents and resolved != root:
        raise HTTPException(status_code=404, detail="Not found")

    safe_name = safe_filename(filename or "file")
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{safe_name}"',
        "Cache-Control": "private, max-age=3600",
    }
    return FileResponse(
        path=str(resolved),
        media_type=content_type or "application/octet-stream",
        headers=headers,
    )
