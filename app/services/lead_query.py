"""Tenant-safe lead list query: filters, search, pagination, sorting."""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.config import settings
from app.models.common import serialize
from app.security.validation import escape_regex
from app.services.lead_service import _norm_phone, _takeover_defaults

ALLOWED_SORT = frozenset(
    {"created_at", "updated_at", "name", "lead_score", "last_inbound_at"}
)
ALLOWED_SORT_ORDER = frozenset({"asc", "desc"})
ALLOWED_SCORE = frozenset({"hot", "warm", "cold"})
ALLOWED_CONSENT = frozenset({"unknown", "pending", "opted_in", "opted_out"})

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def parse_bool_filter(raw: Optional[str]) -> Optional[bool]:
    if raw is None or raw == "":
        return None
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "blacklisted", "blocked"):
        return True
    if v in ("0", "false", "no", "not_blacklisted", "clear"):
        return False
    raise HTTPException(status_code=400, detail="Invalid boolean filter value")


def parse_iso_dt(raw: Optional[str], *, field: str) -> Optional[datetime]:
    if not raw or not str(raw).strip():
        return None
    text = str(raw).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field} datetime") from exc


def sanitize_search(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    max_len = max(1, int(settings.LEAD_SEARCH_MAX_LENGTH))
    if len(text) > max_len:
        raise HTTPException(status_code=400, detail=f"search max length is {max_len}")
    # Reject NoSQL-ish payloads
    if text.startswith("$") or text.startswith("{") or text.startswith("["):
        raise HTTPException(status_code=400, detail="Invalid search")
    return text


def build_lead_filter(
    user_id: str,
    *,
    search: Optional[str] = None,
    score: Optional[str] = None,
    consent_status: Optional[str] = None,
    blacklist_status: Optional[str] = None,
    agent_id: Optional[str] = None,
    source: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    updated_from: Optional[str] = None,
    updated_to: Optional[str] = None,
    last_inbound_from: Optional[str] = None,
    last_inbound_to: Optional[str] = None,
    ai_paused: Optional[str] = None,
    needs_human: Optional[str] = None,
    takeover_active: Optional[str] = None,
    window_status: Optional[str] = None,
    lead_score_min: Optional[int] = None,
    lead_score_max: Optional[int] = None,
) -> dict[str, Any]:
    q: dict[str, Any] = {"user_id": user_id}

    if score:
        if not isinstance(score, str):
            raise HTTPException(status_code=400, detail="Invalid score filter")
        s = score.strip().lower()
        if s not in ALLOWED_SCORE:
            raise HTTPException(status_code=400, detail="Invalid score filter")
        q["score"] = s

    if consent_status:
        c = consent_status.strip().lower()
        if c not in ALLOWED_CONSENT:
            raise HTTPException(status_code=400, detail="Invalid consent_status")
        if c == "unknown":
            q.setdefault("$and", []).append(
                {
                    "$or": [
                        {"whatsapp_consent_status": "unknown"},
                        {"whatsapp_consent_status": {"$exists": False}},
                        {"whatsapp_consent_status": None},
                    ]
                }
            )
        else:
            q["whatsapp_consent_status"] = c

    bl = parse_bool_filter(blacklist_status)
    if bl is not None:
        q["blacklisted"] = bl

    paused = parse_bool_filter(ai_paused)
    if paused is not None:
        q["ai_paused"] = paused

    nh = parse_bool_filter(needs_human)
    if nh is not None:
        q["needs_human"] = nh

    takeover = parse_bool_filter(takeover_active)
    if takeover is True:
        q["takeover_by"] = {"$nin": [None, ""]}
    elif takeover is False:
        q.setdefault("$and", []).append(
            {"$or": [{"takeover_by": None}, {"takeover_by": {"$exists": False}}, {"takeover_by": ""}]}
        )

    if agent_id:
        aid = agent_id.strip()
        if not aid or len(aid) > 64:
            raise HTTPException(status_code=400, detail="Invalid agent_id")
        q["assigned_agent_id"] = aid

    if source:
        src = source.strip()[:100]
        if src.startswith("$"):
            raise HTTPException(status_code=400, detail="Invalid source")
        q["source"] = src

    def _range(field: str, frm: Optional[str], to: Optional[str]) -> None:
        gte = parse_iso_dt(frm, field=f"{field}_from")
        lte = parse_iso_dt(to, field=f"{field}_to")
        if gte or lte:
            rng: dict[str, Any] = {}
            if gte:
                rng["$gte"] = gte
            if lte:
                rng["$lte"] = lte
            q[field] = rng

    _range("created_at", created_from, created_to)
    _range("updated_at", updated_from, updated_to)
    _range("last_inbound_at", last_inbound_from, last_inbound_to)

    if lead_score_min is not None or lead_score_max is not None:
        rng = {}
        if lead_score_min is not None:
            rng["$gte"] = int(lead_score_min)
        if lead_score_max is not None:
            rng["$lte"] = int(lead_score_max)
        q["lead_score"] = rng

    # Window open/closed: approximate via expires_at vs now (exact open needs Python check post-fetch)
    if window_status:
        ws = window_status.strip().lower()
        now = datetime.now(timezone.utc)
        if ws == "open":
            q["whatsapp_window_expires_at"] = {"$gt": now}
        elif ws == "closed":
            q.setdefault("$and", []).append(
                {
                    "$or": [
                        {"whatsapp_window_expires_at": {"$lte": now}},
                        {"whatsapp_window_expires_at": None},
                        {"whatsapp_window_expires_at": {"$exists": False}},
                    ]
                }
            )
        else:
            raise HTTPException(status_code=400, detail="window_status must be open or closed")

    term = sanitize_search(search)
    if term:
        phone_norm = _norm_phone(term)
        escaped = escape_regex(term)
        ors: list[dict[str, Any]] = [
            {"name": {"$regex": escaped, "$options": "i"}},
            {"email": {"$regex": escaped, "$options": "i"}},
            {"company": {"$regex": escaped, "$options": "i"}},
            {"source": {"$regex": escaped, "$options": "i"}},
            {"tags": {"$regex": escaped, "$options": "i"}},
        ]
        if phone_norm and phone_norm.startswith("+") and len(phone_norm) >= 8:
            ors.append({"phone": phone_norm})
            ors.append({"phone": {"$regex": "^" + escape_regex(phone_norm)}})
        else:
            digits = re.sub(r"\D", "", term)
            if len(digits) >= 6:
                ors.append({"phone": {"$regex": escape_regex(digits)}})
        q.setdefault("$and", []).append({"$or": ors})

    return q


def pagination_params(
    page: int = 1,
    page_size: int | None = None,
) -> tuple[int, int, int]:
    default_ps = max(1, int(settings.LEAD_LIST_DEFAULT_PAGE_SIZE))
    max_ps = max(1, int(settings.LEAD_LIST_MAX_PAGE_SIZE))
    ps = default_ps if page_size is None else int(page_size)
    if page < 1:
        raise HTTPException(status_code=400, detail="page must be >= 1")
    if ps < 1 or ps > max_ps:
        raise HTTPException(status_code=400, detail=f"page_size must be 1–{max_ps}")
    skip = (page - 1) * ps
    return page, ps, skip


def sort_spec(sort_by: Optional[str], sort_order: Optional[str]) -> list[tuple[str, int]]:
    field = (sort_by or "updated_at").strip()
    if field not in ALLOWED_SORT:
        raise HTTPException(status_code=400, detail="Invalid sort_by")
    order = (sort_order or "desc").strip().lower()
    if order not in ALLOWED_SORT_ORDER:
        raise HTTPException(status_code=400, detail="sort_order must be asc or desc")
    direction = 1 if order == "asc" else -1
    # Stable secondary sort on _id
    return [(field, direction), ("_id", direction)]


async def query_leads_page(
    db,
    user_id: str,
    *,
    page: int = 1,
    page_size: int | None = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    **filters: Any,
) -> dict[str, Any]:
    page, ps, skip = pagination_params(page, page_size)
    filt = build_lead_filter(user_id, **filters)
    sort = sort_spec(sort_by, sort_order)

    total = await db.leads.count_documents(filt)
    total_pages = math.ceil(total / ps) if total else 0
    cursor = db.leads.find(filt).sort(sort).skip(skip).limit(ps)
    items = [_takeover_defaults(d) async for d in cursor]

    return {
        "items": [serialize(d) for d in items],
        "page": page,
        "page_size": ps,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1 and total > 0,
    }


async def iter_leads_for_export(db, user_id: str, *, max_rows: int, **filters: Any):
    filt = build_lead_filter(user_id, **filters)
    total = await db.leads.count_documents(filt)
    if total > max_rows:
        raise HTTPException(
            status_code=400,
            detail=f"Export exceeds limit of {max_rows} rows ({total} matched). Narrow filters.",
        )
    cursor = db.leads.find(filt).sort([("updated_at", -1), ("_id", -1)]).limit(max_rows)
    async for doc in cursor:
        yield _takeover_defaults(doc)


def csv_safe_cell(value: Any) -> str:
    """Neutralise CSV formula injection."""
    if value is None:
        return ""
    text = str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def is_valid_email(raw: str) -> bool:
    return bool(_EMAIL_RE.match((raw or "").strip())) and len(raw) <= 200
