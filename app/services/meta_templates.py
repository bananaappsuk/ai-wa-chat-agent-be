"""Meta Cloud API template sync, schema, and Graph component mapping (Phase 2F).

Does not create templates in Graph. Token is never logged.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException

from app.config import settings
from app.models.common import utcnow
from app.services.whatsapp_template_approval import (
    is_whatsapp_template_sendable,
    normalize_whatsapp_approval_status,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r"\{\{(\d+)\}\}")
_MAX_PAGES = 20
_GRAPH_HOSTS = ("graph.facebook.com", "graph.instagram.com")


class MetaTemplateError(ValueError):
    """Invalid Meta template selection or variable mapping (HTTP 400)."""


def assert_meta_connected_for_templates(user: dict) -> None:
    from app.services.meta_credentials import MetaCredentialsError, get_meta_credentials_for_user

    try:
        creds = get_meta_credentials_for_user(user)
    except MetaCredentialsError as exc:
        msg = str(exc)
        code = 403 if "does not match" in msg.lower() or "not connected" in msg.lower() else 400
        raise HTTPException(status_code=code, detail=msg) from exc
    if not (creds.waba_id or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Meta WhatsApp templates are not configured (WABA missing).",
        )


def assert_poc_meta_template_tenant(user: dict) -> None:
    """Compatibility alias — tenant credential check (no env PNID gate)."""
    assert_meta_connected_for_templates(user)


def _timeout() -> float:
    return max(5.0, float(settings.META_HTTP_TIMEOUT_SECONDS or 30.0))


def _graph_version() -> str:
    return (settings.META_GRAPH_VERSION or "v21.0").strip().lstrip("/")


def _auth_headers(access_token: str) -> dict[str, str]:
    token = (access_token or "").strip()
    if not token:
        raise MetaTemplateError("Meta credentials are not configured")
    return {"Authorization": f"Bearer {token}"}


def _safe_graph_url(url: str) -> str:
    raw = (url or "").strip()
    parsed = urlparse(raw)
    if (parsed.scheme or "").lower() != "https":
        raise MetaTemplateError("Invalid Graph paging URL")
    host = (parsed.hostname or "").lower()
    if host not in _GRAPH_HOSTS:
        raise MetaTemplateError("Invalid Graph paging host")
    return raw


def _sanitize_button(btn: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ("type", "text", "url", "phone_number", "example"):
        if k in btn and btn[k] is not None:
            val = btn[k]
            if k == "example" and isinstance(val, list):
                out[k] = [str(x)[:200] for x in val[:5]]
            elif isinstance(val, str):
                out[k] = val[:500]
            else:
                out[k] = val
    return out


def sanitize_components(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw[:20]:
        if not isinstance(item, dict):
            continue
        ctype = str(item.get("type") or "").strip().upper()
        if not ctype:
            continue
        row: dict[str, Any] = {"type": ctype}
        fmt = item.get("format")
        if isinstance(fmt, str) and fmt.strip():
            row["format"] = fmt.strip().upper()[:32]
        text = item.get("text")
        if isinstance(text, str):
            row["text"] = text[:4096]
        buttons = item.get("buttons")
        if isinstance(buttons, list):
            row["buttons"] = [
                _sanitize_button(b) for b in buttons[:10] if isinstance(b, dict)
            ]
        out.append(row)
    return out


def _placeholder_keys(text: str) -> list[str]:
    found = sorted({int(m.group(1)) for m in _PLACEHOLDER.finditer(text or "")})
    return [str(n) for n in found]


def analyze_send_support(components: list[dict[str, Any]], category: str | None) -> tuple[bool, str | None]:
    cat = (category or "").strip().upper()
    if cat in ("AUTHENTICATION", "AUTH"):
        return False, "Authentication templates are not supported in this version."
    for comp in components:
        ctype = str(comp.get("type") or "").upper()
        fmt = str(comp.get("format") or "").upper()
        if ctype == "HEADER" and fmt in ("IMAGE", "VIDEO", "DOCUMENT", "LOCATION"):
            return False, "Media header templates are not supported in this version."
        if ctype == "CAROUSEL" or ctype == "LIMITED_TIME_OFFER":
            return False, "This template type is not supported in this version."
        for btn in comp.get("buttons") or []:
            if not isinstance(btn, dict):
                continue
            btype = str(btn.get("type") or "").upper()
            if btype in ("COPY_CODE", "OTP", "VOICE_CALL", "FLOW", "MPM", "CATALOG", "ORDER_DETAILS"):
                return False, "This template button type is not supported in this version."
    return True, None


def build_variable_schema(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    schema: list[dict[str, Any]] = []
    for comp in components:
        ctype = str(comp.get("type") or "").upper()
        if ctype == "BODY":
            for key in _placeholder_keys(str(comp.get("text") or "")):
                schema.append(
                    {
                        "key": key,
                        "kind": "body",
                        "index": int(key) - 1,
                        "param_type": "text",
                        "required": True,
                    }
                )
        elif ctype == "HEADER" and str(comp.get("format") or "").upper() in ("", "TEXT"):
            keys = _placeholder_keys(str(comp.get("text") or ""))
            if keys:
                schema.append(
                    {
                        "key": "header",
                        "kind": "header",
                        "index": 0,
                        "param_type": "text",
                        "required": True,
                    }
                )
        elif ctype == "BUTTONS":
            for i, btn in enumerate(comp.get("buttons") or []):
                if not isinstance(btn, dict):
                    continue
                if str(btn.get("type") or "").upper() != "URL":
                    continue
                url = str(btn.get("url") or "")
                if _placeholder_keys(url):
                    schema.append(
                        {
                            "key": f"button:{i}",
                            "kind": "button",
                            "index": i,
                            "sub_type": "url",
                            "param_type": "text",
                            "required": True,
                        }
                    )
    return schema


def map_graph_status(raw: str | None) -> str:
    s = (raw or "").strip().upper().replace(" ", "_")
    aliases = {
        "APPROVED": "approved",
        "PENDING": "pending",
        "IN_APPEAL": "under_review",
        "REJECTED": "rejected",
        "PAUSED": "paused",
        "DISABLED": "paused",
        "FLAGGED": "under_review",
        "PENDING_DELETION": "paused",
        "DELETED": "rejected",
        "LIMIT_EXCEEDED": "paused",
    }
    if s in aliases:
        return normalize_whatsapp_approval_status(aliases[s])
    return normalize_whatsapp_approval_status(raw)


def is_meta_template_sendable(doc: dict) -> bool:
    if (doc.get("provider") or "") != "meta":
        return False
    if not doc.get("send_supported"):
        return False
    if not (doc.get("meta_template_name") or "").strip():
        return False
    if not (doc.get("meta_language_code") or "").strip():
        return False
    return is_whatsapp_template_sendable(doc.get("whatsapp_approval_status"))


def build_graph_components(*, template: dict, content_variables: dict[str, str] | None) -> list[dict[str, Any]]:
    if not is_meta_template_sendable(template):
        raise MetaTemplateError("This Meta template cannot be sent (not approved or not supported).")
    vars_in = {str(k): str(v) for k, v in (content_variables or {}).items()}
    schema = template.get("variable_schema") or []
    if not isinstance(schema, list):
        schema = []
    required = [s for s in schema if isinstance(s, dict) and s.get("required")]
    extra = set(vars_in.keys()) - {str(s.get("key")) for s in required}
    if extra:
        raise MetaTemplateError("Unexpected template variables: " + ", ".join(sorted(extra)[:12]))
    missing = [str(s.get("key")) for s in required if not (vars_in.get(str(s.get("key"))) or "").strip()]
    if missing:
        raise MetaTemplateError("Missing required template variables: " + ", ".join(missing[:12]))

    body_params: list[dict[str, str]] = []
    header_text: str | None = None
    buttons: dict[int, str] = {}
    body_slots = sorted(
        [s for s in required if s.get("kind") == "body"],
        key=lambda s: int(s.get("index") or 0),
    )
    for slot in body_slots:
        body_params.append({"type": "text", "text": vars_in[str(slot["key"])].strip()})
    for slot in required:
        kind = slot.get("kind")
        if kind == "header":
            header_text = vars_in[str(slot["key"])].strip()
        elif kind == "button":
            buttons[int(slot.get("index") or 0)] = vars_in[str(slot["key"])].strip()

    components: list[dict[str, Any]] = []
    if header_text is not None:
        components.append(
            {"type": "header", "parameters": [{"type": "text", "text": header_text}]}
        )
    if body_params:
        components.append({"type": "body", "parameters": body_params})
    for idx in sorted(buttons):
        components.append(
            {
                "type": "button",
                "sub_type": "url",
                "index": str(idx),
                "parameters": [{"type": "text", "text": buttons[idx]}],
            }
        )
    return components


def _graph_item_to_row(item: dict[str, Any], *, user_id: str) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    language = str(item.get("language") or "").strip()
    if not name or not language:
        return None
    raw_status = str(item.get("status") or "")
    norm = map_graph_status(raw_status)
    category = str(item.get("category") or "").strip() or None
    components = sanitize_components(item.get("components"))
    send_ok, send_reason = analyze_send_support(components, category)
    schema = build_variable_schema(components)
    variables = [str(s.get("key")) for s in schema if s.get("key")]
    local_status = "approved" if norm == "approved" else (
        "rejected" if norm == "rejected" else "pending"
    )
    now = utcnow()
    return {
        "user_id": user_id,
        "provider": "meta",
        "name": name,
        "meta_template_name": name,
        "meta_language_code": language,
        "language": language,
        "meta_graph_id": str(item.get("id") or "").strip() or None,
        "status": local_status,
        "whatsapp_approval_status": norm,
        "whatsapp_approval_status_raw": raw_status or None,
        "whatsapp_category": category,
        "components": components,
        "variable_schema": schema,
        "variables": variables,
        "send_supported": send_ok,
        "send_unsupported_reason": send_reason,
        "content_sid": None,
        "updated_at": now,
    }


async def fetch_graph_message_templates(user: dict) -> list[dict[str, Any]]:
    from app.services.meta_credentials import MetaCredentialsError, get_meta_credentials_for_user

    try:
        creds = get_meta_credentials_for_user(user)
    except MetaCredentialsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    waba = (creds.waba_id or "").strip()
    if not waba or "/" in waba or ".." in waba:
        raise HTTPException(status_code=400, detail="Meta WABA ID is not configured")
    version = _graph_version()
    url = (
        f"https://graph.facebook.com/{version}/{waba}/message_templates"
        f"?fields=id,name,language,status,category,components&limit=100"
    )
    items: list[dict[str, Any]] = []
    timeout = httpx.Timeout(_timeout())
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _ in range(_MAX_PAGES):
            resp = await client.get(_safe_graph_url(url), headers=_auth_headers(creds.access_token))
            try:
                data = resp.json()
            except Exception:
                data = {}
            if resp.is_error or not isinstance(data, dict):
                err = data.get("error") if isinstance(data, dict) else None
                msg = "Meta template list failed"
                if isinstance(err, dict):
                    msg = str(err.get("message") or msg)[:200]
                logger.warning("meta_template_list failed status=%s", resp.status_code)
                raise HTTPException(status_code=502, detail=msg)
            for row in data.get("data") or []:
                if isinstance(row, dict):
                    items.append(row)
            paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
            nxt = str(paging.get("next") or "").strip()
            if not nxt:
                break
            url = nxt
    return items


async def sync_meta_templates_for_user(db, *, user: dict) -> dict[str, Any]:
    assert_meta_connected_for_templates(user)
    user_id = str(user["_id"])
    try:
        graph_rows = await fetch_graph_message_templates(user)
    except HTTPException:
        raise
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Meta template list timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Meta template list request failed") from exc

    upserted = 0
    skipped = 0
    now = utcnow()
    for item in graph_rows:
        row = _graph_item_to_row(item, user_id=user_id)
        if not row:
            skipped += 1
            continue
        filt = {
            "user_id": user_id,
            "provider": "meta",
            "meta_template_name": row["meta_template_name"],
            "meta_language_code": row["meta_language_code"],
        }
        set_doc = dict(row)
        created = now
        existing = await db.templates.find_one(filt, {"created_at": 1})
        if existing and existing.get("created_at"):
            created = existing["created_at"]
        set_doc["created_at"] = created
        await db.templates.update_one(filt, {"$set": set_doc}, upsert=True)
        upserted += 1
    if user.get("_id") is not None:
        await db.users.update_one(
            {"_id": user["_id"]},
            {"$set": {"meta_last_template_sync_at": now, "updated_at": now}},
        )
    return {"ok": True, "synced": upserted, "skipped": skipped, "fetched": len(graph_rows)}
