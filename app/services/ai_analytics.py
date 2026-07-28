"""Tenant-safe AI analytics aggregates."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.models.common import serialize


def _parse_range(date_from: Optional[str], date_to: Optional[str]) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    end = now
    start = now - timedelta(days=30)
    if date_from:
        try:
            start = datetime.fromisoformat(date_from.replace("Z", "+00:00"))
        except ValueError:
            pass
    if date_to:
        try:
            end = datetime.fromisoformat(date_to.replace("Z", "+00:00"))
        except ValueError:
            pass
    if end - start > timedelta(days=90):
        start = end - timedelta(days=90)
    return start, end


async def overview(db, *, tenant_id: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    start, end = _parse_range(date_from, date_to)
    filt = {"tenant_id": tenant_id, "created_at": {"$gte": start, "$lte": end}}
    total = await db.ai_usage.count_documents(filt)
    ok = await db.ai_usage.count_documents({**filt, "success": True})
    fail = total - ok
    pipeline = [
        {"$match": filt},
        {
            "$group": {
                "_id": None,
                "input_tokens": {"$sum": "$input_tokens"},
                "output_tokens": {"$sum": "$output_tokens"},
                "cost": {"$sum": "$estimated_cost"},
                "latency_sum": {"$sum": "$latency_ms"},
                "latency_n": {"$sum": 1},
            }
        },
    ]
    agg = await db.ai_usage.aggregate(pipeline).to_list(1)
    g = agg[0] if agg else {}
    latency_n = int(g.get("latency_n") or 0) or 1
    replies = await db.ai_usage.count_documents({**filt, "operation": "reply"})
    summaries = await db.ai_usage.count_documents({**filt, "operation": "summary"})
    extractions = await db.ai_usage.count_documents({**filt, "operation": "extraction"})
    accepted = await db.ai_suggestions.count_documents(
        {"tenant_id": tenant_id, "status": "accepted", "reviewed_at": {"$gte": start, "$lte": end}}
    )
    rejected = await db.ai_suggestions.count_documents(
        {"tenant_id": tenant_id, "status": "rejected", "reviewed_at": {"$gte": start, "$lte": end}}
    )
    escalated = await db.leads.count_documents(
        {"user_id": tenant_id, "needs_human": True, "updated_at": {"$gte": start, "$lte": end}}
    )
    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "replies_generated": replies,
        "success_count": ok,
        "failure_count": fail,
        "success_rate": round(ok / total, 4) if total else 0,
        "avg_latency_ms": round(float(g.get("latency_sum") or 0) / latency_n, 1),
        "input_tokens": int(g.get("input_tokens") or 0),
        "output_tokens": int(g.get("output_tokens") or 0),
        "estimated_cost": round(float(g.get("cost") or 0), 4),
        "summaries_generated": summaries,
        "extractions_generated": extractions,
        "suggestions_accepted": accepted,
        "suggestions_rejected": rejected,
        "conversations_escalated": escalated,
    }


async def usage_page(
    db,
    *,
    tenant_id: str,
    page: int = 1,
    page_size: int = 25,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    start, end = _parse_range(date_from, date_to)
    filt = {"tenant_id": tenant_id, "created_at": {"$gte": start, "$lte": end}}
    page = max(1, page)
    page_size = min(100, max(1, page_size))
    total = await db.ai_usage.count_documents(filt)
    items = []
    async for d in (
        db.ai_usage.find(filt, {"metadata.prompt": 0})
        .sort("created_at", -1)
        .skip((page - 1) * page_size)
        .limit(page_size)
    ):
        s = serialize(d)
        # Never expose message bodies
        s.pop("prompt", None)
        items.append(s)
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
    }


async def outcomes(db, *, tenant_id: str, date_from: Optional[str] = None, date_to: Optional[str] = None) -> dict:
    start, end = _parse_range(date_from, date_to)
    intent_pipe = [
        {
            "$match": {
                "user_id": tenant_id,
                "classified_at": {"$gte": start, "$lte": end},
                "current_intent": {"$exists": True},
            }
        },
        {"$group": {"_id": "$current_intent", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 20},
    ]
    sent_pipe = [
        {
            "$match": {
                "user_id": tenant_id,
                "classified_at": {"$gte": start, "$lte": end},
                "current_sentiment": {"$exists": True},
            }
        },
        {"$group": {"_id": "$current_sentiment", "count": {"$sum": 1}}},
    ]
    model_pipe = [
        {"$match": {"tenant_id": tenant_id, "created_at": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$model", "count": {"$sum": 1}, "tokens": {"$sum": "$total_tokens"}}},
        {"$sort": {"count": -1}},
        {"$limit": 10},
    ]
    err_pipe = [
        {
            "$match": {
                "tenant_id": tenant_id,
                "created_at": {"$gte": start, "$lte": end},
                "success": False,
                "error_category": {"$ne": None},
            }
        },
        {"$group": {"_id": "$error_category", "count": {"$sum": 1}}},
    ]
    intents = [{"intent": x["_id"], "count": x["count"]} async for x in db.leads.aggregate(intent_pipe)]
    sentiments = [
        {"sentiment": x["_id"], "count": x["count"]} async for x in db.leads.aggregate(sent_pipe)
    ]
    models = [
        {"model": x["_id"], "count": x["count"], "tokens": x.get("tokens") or 0}
        async for x in db.ai_usage.aggregate(model_pipe)
    ]
    errors = [
        {"category": x["_id"], "count": x["count"]} async for x in db.ai_usage.aggregate(err_pipe)
    ]
    moderation_blocks = await db.ai_events.count_documents(
        {
            "tenant_id": tenant_id,
            "event_type": "moderation_block",
            "created_at": {"$gte": start, "$lte": end},
        }
    )
    fallbacks = await db.ai_usage.count_documents(
        {
            "tenant_id": tenant_id,
            "created_at": {"$gte": start, "$lte": end},
            "metadata.used_fallback": True,
        }
    )
    return {
        "intent_distribution": intents,
        "sentiment_distribution": sentiments,
        "model_usage": models,
        "provider_errors": errors,
        "moderation_blocks": moderation_blocks,
        "fallback_usage": fallbacks,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
    }
