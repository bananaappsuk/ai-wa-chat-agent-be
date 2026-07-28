"""Media storage abstraction — local filesystem now, swap for S3/Cloudinary later."""
from __future__ import annotations

import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Optional, Tuple

from app.config import settings

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str) -> str:
    base = Path(name or "file").name
    # Collapse double extensions that look executable/scripty
    cleaned = _SAFE_NAME.sub("_", base).strip("._") or "file"
    lower = cleaned.lower()
    for bad in (".html", ".htm", ".svg", ".js", ".php", ".exe", ".bat", ".cmd", ".sh"):
        if bad in lower:
            cleaned = cleaned.replace(bad, "_")
            cleaned = cleaned.replace(bad.upper(), "_")
    return cleaned[:180]


class MediaStorage(ABC):
    @abstractmethod
    def save(
        self,
        *,
        user_id: str,
        filename: str,
        content_type: str,
        data: bytes | BinaryIO,
    ) -> dict:
        """Persist bytes. Returns metadata including public `url` and `storage_key`."""

    @abstractmethod
    def open(self, storage_key: str) -> tuple[BinaryIO, str, Optional[str]]:
        """Return (stream, content_type, filename)."""

    @abstractmethod
    def exists(self, storage_key: str) -> bool:
        ...


class LocalMediaStorage(MediaStorage):
    def __init__(self, root: Optional[str] = None) -> None:
        self.root = Path(root or settings.MEDIA_STORAGE_DIR).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _public_base(self) -> str:
        base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
        return base

    def save(
        self,
        *,
        user_id: str,
        filename: str,
        content_type: str,
        data: bytes | BinaryIO,
    ) -> dict:
        file_id = uuid.uuid4().hex
        safe = safe_filename(filename)
        rel = f"{user_id}/{file_id}_{safe}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = data.read() if hasattr(data, "read") else data  # type: ignore[union-attr]
        if not isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw)
        path.write_bytes(raw)

        public_path = f"/api/media/files/{file_id}"
        base = self._public_base()
        url = f"{base}{public_path}" if base else public_path

        # Sidecar metadata for content-type / original name lookup
        meta_path = self.root / f"{user_id}/{file_id}.meta"
        meta_path.write_text(
            f"{content_type}\n{safe}\n{rel}\n",
            encoding="utf-8",
        )
        return {
            "id": file_id,
            "storage_key": file_id,
            "filename": safe,
            "content_type": content_type,
            "size": len(raw),
            "url": url,
            "path": public_path,
        }

    def _meta(self, file_id: str) -> tuple[str, str, Path]:
        # Scan user dirs for meta (dev-scale). Keys are UUIDs — unguessable.
        for meta in self.root.glob(f"*/{file_id}.meta"):
            lines = meta.read_text(encoding="utf-8").splitlines()
            content_type = lines[0] if lines else "application/octet-stream"
            filename = lines[1] if len(lines) > 1 else file_id
            rel = lines[2] if len(lines) > 2 else ""
            path = self.root / rel if rel else meta.with_name(f"{file_id}_file")
            if not path.exists():
                # Fallback: first matching binary
                matches = list(meta.parent.glob(f"{file_id}_*"))
                matches = [m for m in matches if m.suffix != ".meta" and m.is_file()]
                if not matches:
                    raise FileNotFoundError(file_id)
                path = matches[0]
            return content_type, filename, path
        raise FileNotFoundError(file_id)

    def open(self, storage_key: str) -> tuple[BinaryIO, str, Optional[str]]:
        content_type, filename, path = self._meta(storage_key)
        return path.open("rb"), content_type, filename

    def resolve_path(self, storage_key: str) -> tuple[str, Optional[str], Path]:
        content_type, filename, path = self._meta(storage_key)
        return content_type, filename, path

    def exists(self, storage_key: str) -> bool:
        try:
            self._meta(storage_key)
            return True
        except FileNotFoundError:
            return False


_storage: MediaStorage | None = None


def get_media_storage() -> MediaStorage:
    global _storage
    if _storage is None:
        _storage = LocalMediaStorage()
    return _storage
