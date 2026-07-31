"""MIME sniffing helpers (no external libmagic dependency)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# Magic signatures → MIME
_SIGNATURES: list[tuple[bytes, str]] = [
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF", "application/pdf"),
    (b"\x1aE\xdf\xa3", "video/webm"),
    (b"ID3", "audio/mpeg"),
    (b"\xff\xfb", "audio/mpeg"),
    (b"\xff\xf3", "audio/mpeg"),
    (b"\xff\xf2", "audio/mpeg"),
    (b"OggS", "audio/ogg"),
]

_EXT_TO_MIME: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".mp3": "audio/mpeg",
    ".mpeg": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".aac": "audio/aac",
    ".amr": "audio/amr",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".3gp": "video/3gpp",
}

_ALIASES = {
    "image/jpg": "image/jpeg",
    "audio/mp3": "audio/mpeg",
}

# Types we allow by extension when magic bytes are ambiguous / missing
_EXTENSION_FALLBACK_OK = {
    "audio/aac",
    "audio/amr",
    "audio/mp4",
    "audio/opus",
    "audio/ogg",
    "audio/mpeg",
    "application/msword",
    "application/vnd.ms-excel",
    "text/plain",
}


def _normalize(mime: Optional[str]) -> str:
    raw = (mime or "").split(";")[0].strip().lower()
    return _ALIASES.get(raw, raw)


def sniff_mime(data: bytes) -> Optional[str]:
    if not data:
        return None
    head = data[:64]
    # RIFF container: WEBP image or WAVE audio (never treat WAVE as webp)
    if head.startswith(b"RIFF") and len(data) >= 12:
        kind = data[8:12]
        if kind == b"WEBP":
            return "image/webp"
        if kind == b"WAVE":
            return "audio/wav"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        # MP4 / 3GP / M4A family
        brand = data[8:12] if len(data) >= 12 else b""
        if brand in (b"3gp4", b"3gp5", b"3g2a"):
            return "video/3gpp"
        if brand in (b"M4A ", b"M4B ", b"mp4a"):
            return "audio/mp4"
        return "video/mp4"
    for sig, mime in _SIGNATURES:
        if head.startswith(sig):
            return mime
    # OLE Compound Document (.doc / .xls)
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/msword"
    # Office Open XML / zip-based
    if data[:2] == b"PK":
        return "application/zip"
    # UTF-8 / ASCII plain text heuristic for text/plain allow-list
    try:
        sample = data[:2048].decode("utf-8")
        if "\x00" not in sample:
            return "text/plain"
    except UnicodeDecodeError:
        pass
    return None


def mime_from_filename(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    ext = Path(filename).suffix.lower()
    return _EXT_TO_MIME.get(ext)


def _compatible(declared: str, sniffed: str) -> bool:
    if declared == sniffed:
        return True
    # Browser often mislabels M4A as audio/mp4 while bytes look like video/mp4 ftyp
    if {declared, sniffed} <= {"audio/mp4", "video/mp4"}:
        return True
    # ZIP sniff for OOXML office docs
    if sniffed == "application/zip" and declared in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/msword",
        "application/vnd.ms-excel",
    }:
        return True
    # Legacy OLE sniffed as msword may be declared as excel
    if sniffed == "application/msword" and declared in {
        "application/msword",
        "application/vnd.ms-excel",
    }:
        return True
    return False


def mime_matches_declared(data: bytes, declared: str, filename: Optional[str] = None) -> bool:
    return resolve_upload_mime(data, declared, filename) is not None


def resolve_upload_mime(
    data: bytes,
    declared: Optional[str],
    filename: Optional[str] = None,
) -> Optional[str]:
    """
    Return the MIME type to store for an upload, or None if the file should be rejected.

    Prefer sniffed content when reliable; fall back to filename extension when browsers
    send empty/octet-stream or for formats without stable magic headers.
    """
    declared_n = _normalize(declared)
    if declared_n in ("", "application/octet-stream", "binary/octet-stream"):
        declared_n = ""
    sniffed = sniff_mime(data)
    sniffed_n = _normalize(sniffed) if sniffed else None
    from_name = mime_from_filename(filename)

    # Strong match: bytes agree with declared type
    if sniffed_n and declared_n and _compatible(declared_n, sniffed_n):
        # Prefer declared when it's the more specific office/audio label
        if sniffed_n == "application/zip" and declared_n:
            return declared_n
        if sniffed_n == "video/mp4" and declared_n == "audio/mp4":
            return "audio/mp4"
        if sniffed_n == "application/msword" and declared_n == "application/vnd.ms-excel":
            return declared_n
        return sniffed_n if sniffed_n != "application/zip" else declared_n

    # Browser declared wrong type (e.g. jpeg vs png) but content is a known image/pdf/audio
    if sniffed_n and sniffed_n not in ("application/zip", "audio/wav", "video/webm"):
        if not declared_n or declared_n.startswith(sniffed_n.split("/")[0] + "/"):
            return sniffed_n
        # Declared image/*, sniffed another allowed image — trust bytes
        if declared_n.startswith("image/") and sniffed_n.startswith("image/"):
            return sniffed_n
        if declared_n.startswith("audio/") and sniffed_n.startswith("audio/"):
            return sniffed_n
        if declared_n.startswith("video/") and sniffed_n.startswith("video/"):
            return sniffed_n

    # ZIP office docs with matching declared or extension
    if sniffed_n == "application/zip":
        candidate = declared_n or from_name
        if candidate in {
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        }:
            return candidate

    # No/weak magic: allow when extension + declared agree for hard-to-sniff types
    candidate = declared_n or from_name
    if candidate and from_name and _normalize(from_name) == candidate:
        if candidate in _EXTENSION_FALLBACK_OK or sniffed_n is None:
            # Still require extension to map to an allowed WhatsApp type
            if from_name == candidate:
                return candidate

    # Declared empty: use extension if we have one and sniff didn't contradict
    if not declared_n and from_name:
        if sniffed_n is None or _compatible(from_name, sniffed_n):
            return from_name

    return None
