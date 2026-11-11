"""Rule-based lead scoring (0–100 → hot|warm|cold). No OpenAI calls."""
from __future__ import annotations

import re
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

from bson import ObjectId

from app.db.mongo import get_db
from app.models.common import utcnow

PURCHASE_KEYWORDS = (
    "buy",
    "purchase",
    "order",
    "interested",
    "deal",
    "offer",
    "pricing",
    "quote",
    "sign up",
    "signup",
)

BUDGET_KEYWORDS = (
    "budget",
    "price",
    "cost",
    "afford",
    "expensive",
    "cheap",
    "£",
    "$",
    "usd",
    "gbp",
    "how much",
)

BOOKING_KEYWORDS = (
    "book",
    "booking",
    "demo",
    "call",
    "meeting",
    "appointment",
    "schedule",
    "contact me",
    "speak to",
    "talk to",
    "callback",
    "call back",
)

NEGATIVE_KEYWORDS = (
    "stop",
    "unsubscribe",
    "cancel",
    "not interested",
    "no thanks",
    "no thank you",
    "remove me",
    "don't contact",
    "do not contact",
    "leave me alone",
)


SCORING_WINDOW_DAYS = 14
HOT_NEEDS_ACTIVITY_DAYS = 7
COLD_AFTER_DAYS = 30


def map_score_label(lead_score: int) -> str:
    if lead_score >= 70:
        return "hot"
    if lead_score >= 40:
        return "warm"
    return "cold"


def _text_blob(messages: list[dict]) -> str:
    parts = []
    for m in messages:
        if m.get("direction") == "inbound":
            parts.append(str(m.get("message") or ""))
    return " ".join(parts).lower()


def _has_word(text: str, keyword: str) -> bool:
    if not (keyword[0].isalnum() and keyword[-1].isalnum()):
        return keyword in text
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text) is not None


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(_has_word(text, k) for k in keywords)


def _ts(m: dict) -> datetime | None:
    ts = m.get("created_at")
    if not isinstance(ts, datetime):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def calculate_lead_score(
    *,
    blacklisted: bool = False,
    inbound_messages: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
) -> tuple[int, str]:
    """
    Return ``(lead_score 0–100, label hot|warm|cold)``.

    Pure function — safe for unit tests without Mongo/OpenAI.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    if blacklisted:
        return 0, "cold"

    inbound = [m for m in (inbound_messages or []) if m.get("direction") == "inbound"]
    stamps = [t for t in (_ts(m) for m in inbound) if t is not None]
    latest = max(stamps) if stamps else None
    cutoff = now - timedelta(days=SCORING_WINDOW_DAYS)
    recent = [m for m in inbound if (_ts(m) or now) >= cutoff]
    blob = _text_blob(recent)

    if _contains_any(blob, NEGATIVE_KEYWORDS):
        return 0, "cold"

    score = 0

    n = len(recent)
    if n == 0:
        return 0, "cold"
    score += min(40, n * 8)

    if latest is not None:
        age = now - latest
        if age <= timedelta(hours=24):
            score += 25
        elif age <= timedelta(days=7):
            score += 15
        elif age <= timedelta(days=30):
            score += 5

    if _contains_any(blob, PURCHASE_KEYWORDS):
        score += 15
    if _contains_any(blob, BUDGET_KEYWORDS):
        score += 10
    if _contains_any(blob, BOOKING_KEYWORDS):
        score += 20

    if latest is not None:
        age = now - latest
        if age > timedelta(days=COLD_AFTER_DAYS):
            score = min(score, 39)
        elif age > timedelta(days=HOT_NEEDS_ACTIVITY_DAYS):
            score = min(score, 69)

    score = max(0, min(100, score))
    return score, map_score_label(score)


async def recalculate_lead_score(user_id: str, lead_id: str) -> Optional[dict[str, Any]]:
    """Load lead + inbound messages (tenant-scoped), compute score, persist fields."""
    if not ObjectId.is_valid(lead_id):
        return None
    db = get_db()
    lead = await db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if not lead:
        return None

    cur = db.messages.find(
        {"user_id": user_id, "lead_id": lead_id, "direction": "inbound"},
        {"message": 1, "direction": 1, "created_at": 1},
    ).sort("created_at", 1)
    inbound = [m async for m in cur]

    blacklisted = bool(lead.get("blacklisted"))
    if not blacklisted:
        phone = lead.get("phone")
        if phone and await db.blacklist.find_one({"user_id": user_id, "phone": phone}):
            blacklisted = True

    lead_score, label = calculate_lead_score(
        blacklisted=blacklisted,
        inbound_messages=inbound,
    )
    now = utcnow()
    await db.leads.update_one(
        {"_id": ObjectId(lead_id), "user_id": user_id},
        {
            "$set": {
                "lead_score": lead_score,
                "score": label,
                "score_updated_at": now,
                "updated_at": now,
            }
        },
    )
    lead["lead_score"] = lead_score
    lead["score"] = label
    lead["score_updated_at"] = now
    return lead


def rescore_stale_leads(limit: int = 2000) -> int:
    """Hot/warm scores only change when the contact messages, so they would never cool down:
    re-score those older than a day. Leaves `updated_at` alone so chat order doesn't jump."""
    from app.workers.tasks import _db

    db = _db()
    now = datetime.now(timezone.utc)
    stale = db.leads.find(
        {"score": {"$in": ["hot", "warm"]}, "$or": [{"score_updated_at": {"$lt": now - timedelta(days=1)}}, {"score_updated_at": None}]},
        {"user_id": 1, "phone": 1, "blacklisted": 1},
    ).limit(limit)
    done = 0
    for lead in stale:
        uid, lid = lead.get("user_id"), str(lead["_id"])
        inbound = list(db.messages.find({"user_id": uid, "lead_id": lid, "direction": "inbound"}, {"message": 1, "direction": 1, "created_at": 1}))
        blacklisted = bool(lead.get("blacklisted")) or bool(lead.get("phone") and db.blacklist.find_one({"user_id": uid, "phone": lead["phone"]}))
        lead_score, label = calculate_lead_score(blacklisted=blacklisted, inbound_messages=inbound, now=now)
        db.leads.update_one({"_id": lead["_id"]}, {"$set": {"lead_score": lead_score, "score": label, "score_updated_at": now}})
        done += 1
    return done
