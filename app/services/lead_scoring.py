"""Rule-based lead scoring (0–100 → hot|warm|cold). No OpenAI calls."""
from __future__ import annotations

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


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(k in text for k in keywords)


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
    blob = _text_blob(inbound)

    if _contains_any(blob, NEGATIVE_KEYWORDS):
        return 0, "cold"

    score = 0

    # Engagement from inbound volume
    n = len(inbound)
    if n == 0:
        return 0, "cold"
    score += min(40, n * 8)

    # Recency of latest inbound
    latest: datetime | None = None
    for m in inbound:
        ts = m.get("created_at")
        if isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if latest is None or ts > latest:
                latest = ts
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
