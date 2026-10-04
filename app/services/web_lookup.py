"""Live lookups — current information from the web for a reply (weather, news, scores,
exchange rates, a public place's opening hours…).

The router decides per message whether a lookup is needed and writes the search query; this
module runs ONE OpenAI web search (Responses API `web_search` tool) and returns a short answer
with its sources. The reply is then written from those results.

Guard rails: kill switch, per-tenant daily cap, the tenant's AI quota, a short cache (ten
people asking about today's weather cost one search), and a hard timeout. Search fees are
recorded in the tenant's AI cost. Never raises — on any problem the reply simply says it
can't check right now.

Uses the Responses endpoint over HTTP (the pinned OpenAI SDK predates the Responses API).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from app.config import settings
from app.services.ai_quota import check_quota

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.openai.com/v1/responses"


@dataclass
class LookupResult:
    query: str
    ok: bool
    text: str = ""
    sources: list[str] = field(default_factory=list)
    cached: bool = False
    reason: str = ""  # disabled | over_daily_limit | quota_exceeded | error | empty


def _post(body: dict) -> dict:
    """The only network call (patched in tests)."""
    resp = httpx.post(
        _ENDPOINT,
        headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=float(settings.AI_WEB_LOOKUP_TIMEOUT_SECONDS),
    )
    resp.raise_for_status()
    return resp.json()


def _redis():
    try:
        from app.workers.queue import get_redis

        return get_redis()
    except Exception:
        return None


def _parse(data: dict) -> tuple[str, list[str], int, int, int]:
    text, sources = "", []
    for item in data.get("output") or []:
        if item.get("type") != "message":
            continue
        for c in item.get("content") or []:
            if c.get("type") == "output_text":
                text += c.get("text") or ""
                for a in c.get("annotations") or []:
                    url = a.get("url") if a.get("type") == "url_citation" else None
                    url = (url or "").replace("?utm_source=openai", "").replace("&utm_source=openai", "")
                    if url and url not in sources:
                        sources.append(url)
    calls = sum(1 for i in data.get("output") or [] if i.get("type") == "web_search_call")
    usage = data.get("usage") or {}
    return text.strip(), sources[:3], int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0), calls


def _record(tenant_id: str, tok_in: int, tok_out: int, latency_ms: int, calls: int) -> None:
    try:
        from pymongo import MongoClient

        from app.services.ai_quota import record_usage_sync

        record_usage_sync(
            MongoClient(settings.MONGO_URI)[settings.MONGO_DB],
            tenant_id=tenant_id,
            operation="web_lookup",
            model=settings.AI_WEB_LOOKUP_MODEL,
            input_tokens=tok_in,
            output_tokens=tok_out,
            latency_ms=latency_ms,
            extra_cost=float(settings.AI_WEB_LOOKUP_COST_PER_CALL) * max(1, calls),
            metadata={"searches": calls},
        )
    except Exception:
        logger.debug("lookup usage record failed", exc_info=True)


def live_lookup(query: str, *, tenant_id: Optional[str]) -> LookupResult:
    q = " ".join((query or "").split())[:300]
    if not q:
        return LookupResult(query=q, ok=False, reason="empty")
    if not settings.AI_WEB_LOOKUP_ENABLED or not (settings.OPENAI_API_KEY or "").strip():
        return LookupResult(query=q, ok=False, reason="disabled")

    r = _redis()
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    cache_key = "lookup:cache:" + hashlib.sha1(f"{day}:{q.lower()}".encode()).hexdigest()
    if r is not None:
        try:
            hit = r.get(cache_key)
            if hit:
                d = json.loads(hit)
                return LookupResult(query=q, ok=True, text=d["text"], sources=d["sources"], cached=True)
        except Exception:
            pass

    if tenant_id:
        allowed, _ = check_quota(tenant_id)
        if not allowed:
            return LookupResult(query=q, ok=False, reason="quota_exceeded")
        if r is not None:
            try:
                count_key = f"lookup:count:{tenant_id}:{day}"
                if int(r.incr(count_key)) > int(settings.AI_WEB_LOOKUP_DAILY_LIMIT_PER_TENANT):
                    return LookupResult(query=q, ok=False, reason="over_daily_limit")
                r.expire(count_key, 86400 * 2)
            except Exception:
                pass

    body = {
        "model": settings.AI_WEB_LOOKUP_MODEL,
        "tools": [{
            "type": "web_search",
            "search_context_size": "low",
            "user_location": {"type": "approximate", "country": settings.AI_WEB_LOOKUP_COUNTRY},
        }],
        "input": (
            f"{q}\n\nAnswer with the key current facts in 2–4 short sentences (include figures, "
            f"dates and place names where relevant; use the units and currency usual in "
            f"{settings.AI_WEB_LOOKUP_COUNTRY}). Today is "
            f"{datetime.now(timezone.utc).strftime('%A %d %B %Y')}."
        ),
        "max_output_tokens": 350,
    }
    t0 = time.monotonic()
    try:
        data = _post(body)
        text, sources, tok_in, tok_out, calls = _parse(data)
    except Exception as exc:
        logger.warning("live lookup failed query=%r err=%s", q[:80], type(exc).__name__)
        return LookupResult(query=q, ok=False, reason="error")
    latency = int((time.monotonic() - t0) * 1000)

    if tenant_id:
        _record(tenant_id, tok_in, tok_out, latency, calls)

    if not text:
        return LookupResult(query=q, ok=False, reason="empty")
    if r is not None:
        try:
            r.set(cache_key, json.dumps({"text": text, "sources": sources}), ex=int(settings.AI_WEB_LOOKUP_CACHE_SECONDS))
        except Exception:
            pass
    return LookupResult(query=q, ok=True, text=text, sources=sources)
