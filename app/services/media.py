"""WhatsApp media helpers: MIME allow-list, inbound parse, message_type."""
from __future__ import annotations

from typing import Any, Optional

# WhatsApp-friendly allow-list
ALLOWED_MIME_TYPES: dict[str, str] = {
    "image/jpeg": "image",
    "image/jpg": "image",
    "image/png": "image",
    "image/webp": "image",
    "image/gif": "image",
    "application/pdf": "document",
    "application/msword": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.ms-excel": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "text/plain": "document",
    "audio/mpeg": "audio",
    "audio/mp3": "audio",
    "audio/ogg": "audio",
    "audio/opus": "audio",
    "audio/aac": "audio",
    "audio/amr": "audio",
    "audio/mp4": "audio",
    "video/mp4": "video",
    "video/3gpp": "video",
}

# Per-type caps (bytes)
MAX_BYTES_BY_TYPE: dict[str, int] = {
    "image": 5 * 1024 * 1024,
    "audio": 16 * 1024 * 1024,
    "video": 16 * 1024 * 1024,
    "document": 16 * 1024 * 1024,
}
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


def normalize_mime(content_type: Optional[str]) -> str:
    raw = (content_type or "application/octet-stream").split(";")[0].strip().lower()
    return raw


def message_type_for_mime(content_type: Optional[str]) -> str:
    mime = normalize_mime(content_type)
    return ALLOWED_MIME_TYPES.get(mime, "document" if mime != "application/octet-stream" else "media")


def is_allowed_mime(content_type: Optional[str]) -> bool:
    return normalize_mime(content_type) in ALLOWED_MIME_TYPES


def max_bytes_for_mime(content_type: Optional[str]) -> int:
    kind = message_type_for_mime(content_type)
    return MAX_BYTES_BY_TYPE.get(kind, DEFAULT_MAX_BYTES)


def infer_message_type(*, body: Optional[str], media_items: list[dict]) -> str:
    if not media_items:
        return "text"
    if len(media_items) == 1:
        return message_type_for_mime(media_items[0].get("content_type"))
    return "media"


def parse_inbound_media(params: dict[str, Any]) -> list[dict]:
    """Parse Twilio NumMedia / MediaUrlN / MediaContentTypeN into media_items."""
    try:
        num = int(params.get("NumMedia") or 0)
    except (TypeError, ValueError):
        num = 0
    items: list[dict] = []
    for i in range(max(0, num)):
        url = (params.get(f"MediaUrl{i}") or "").strip()
        if not url:
            continue
        content_type = normalize_mime(params.get(f"MediaContentType{i}"))
        filename = (params.get(f"MediaFilename{i}") or "").strip() or None
        items.append(
            {
                "url": url,
                "content_type": content_type,
                "filename": filename,
                "index": i,
            }
        )
    return items


def media_fields_from_items(media_items: list[dict], body: str = "") -> dict:
    """Top-level convenience fields + message_type for a message document."""
    first = media_items[0] if media_items else None
    return {
        "message_type": infer_message_type(body=body, media_items=media_items),
        "media_items": media_items,
        "media_url": (first or {}).get("url"),
        "media_content_type": (first or {}).get("content_type"),
        "media_filename": (first or {}).get("filename"),
    }
