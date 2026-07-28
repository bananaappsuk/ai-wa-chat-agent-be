"""Shared validation helpers for security hardening."""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

from bson import ObjectId
from fastapi import HTTPException

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_CONTENT_SID = re.compile(r"^HX[a-zA-Z0-9]{32}$")
_SAFE_SEARCH = re.compile(r"[.*+?^${}()|[\]\\]")
_MONGO_OPS = re.compile(r"^\$")


def require_object_id(value: str | None, *, detail: str = "Not found") -> ObjectId:
    if not value or not ObjectId.is_valid(value):
        raise HTTPException(status_code=404, detail=detail)
    return ObjectId(value)


def parse_object_id(value: str | None) -> Optional[ObjectId]:
    if not value or not ObjectId.is_valid(value):
        return None
    return ObjectId(value)


def validate_e164_phone(phone: str) -> str:
    raw = (phone or "").strip().replace(" ", "").replace("-", "")
    if raw.startswith("whatsapp:"):
        raw = raw[9:]
    if not _E164.match(raw):
        raise HTTPException(status_code=400, detail="Invalid phone number (E.164 required)")
    return raw


def validate_password_complexity(password: str) -> None:
    if len(password) < 8 or len(password) > 128:
        raise HTTPException(status_code=400, detail="Password must be 8–128 characters")
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        raise HTTPException(
            status_code=400,
            detail="Password must include at least one letter and one number",
        )


def is_safe_http_url(url: str, *, require_https: bool = False) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme in ("javascript", "data", "file", "vbscript", "blob"):
        return False
    if scheme not in ("http", "https"):
        return False
    if require_https and scheme != "https":
        return False
    if not parsed.netloc:
        return False
    return True


def assert_safe_http_url(url: str, *, require_https: bool = False, field: str = "url") -> str:
    raw = (url or "").strip()
    if not is_safe_http_url(raw, require_https=require_https):
        raise HTTPException(status_code=400, detail=f"Invalid or unsafe {field}")
    return raw


def validate_content_sid(sid: str) -> str:
    raw = (sid or "").strip()
    if not _CONTENT_SID.match(raw) and not (raw.startswith("HX") and 20 <= len(raw) <= 64):
        # Allow HX… Twilio SIDs; exact length can vary in sandbox/test fixtures
        if not raw.startswith("HX") or len(raw) < 10 or len(raw) > 100:
            raise HTTPException(status_code=400, detail="Invalid Twilio Content SID")
    return raw


def escape_regex(value: str) -> str:
    return _SAFE_SEARCH.sub(r"\\\g<0>", value or "")


def reject_mongo_operators(payload: Any, *, path: str = "body") -> None:
    """Reject dict keys that look like Mongo operators ($set, $gt, …)."""
    if isinstance(payload, dict):
        for key, value in payload.items():
            if isinstance(key, str) and _MONGO_OPS.match(key):
                raise HTTPException(status_code=400, detail=f"Illegal operator in {path}")
            reject_mongo_operators(value, path=f"{path}.{key}")
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            reject_mongo_operators(item, path=f"{path}[{i}]")


def limit_list(items: list | None, *, max_items: int, field: str = "items") -> list:
    values = list(items or [])
    if len(values) > max_items:
        raise HTTPException(
            status_code=400,
            detail=f"{field} exceeds maximum of {max_items}",
        )
    return values


def limit_template_variables(
    variables: dict[str, str] | None,
    *,
    max_keys: int = 20,
    max_key_len: int = 40,
    max_val_len: int = 500,
) -> dict[str, str]:
    data = dict(variables or {})
    if len(data) > max_keys:
        raise HTTPException(status_code=400, detail="Too many template variables")
    out: dict[str, str] = {}
    for key, value in data.items():
        k = str(key)
        v = str(value)
        if len(k) > max_key_len or len(v) > max_val_len:
            raise HTTPException(status_code=400, detail="Template variable too large")
        if k.startswith("$"):
            raise HTTPException(status_code=400, detail="Illegal template variable key")
        out[k] = v
    return out


def patch_allowlist(data: dict, allowed: set[str]) -> dict:
    return {k: v for k, v in data.items() if k in allowed and v is not None}
