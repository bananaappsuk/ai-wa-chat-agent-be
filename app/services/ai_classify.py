"""Intent and sentiment classification — rules first, AI optional."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from app.config import settings
from app.services.whatsapp_consent import is_optout_keyword

INTENTS = (
    "greeting",
    "product_enquiry",
    "pricing",
    "booking",
    "purchase",
    "support",
    "complaint",
    "cancellation",
    "opt_out",
    "follow_up",
    "unknown",
)

SENTIMENTS = ("positive", "neutral", "negative", "urgent")

_RULES: list[tuple[str, re.Pattern[str], float]] = [
    ("greeting", re.compile(r"^\s*(hi|hello|hey|good (morning|afternoon|evening))\b", re.I), 0.9),
    ("pricing", re.compile(r"\b(price|pricing|cost|how much|quote|fees?)\b", re.I), 0.85),
    ("booking", re.compile(r"\b(book|schedule|appointment|demo|call|calendar)\b", re.I), 0.85),
    ("purchase", re.compile(r"\b(buy|purchase|order|sign up|subscribe)\b", re.I), 0.8),
    ("cancellation", re.compile(r"\b(cancel|cancellation|unsubscribe me)\b", re.I), 0.85),
    ("complaint", re.compile(r"\b(terrible|awful|angry|frustrated|complaint|not happy|unacceptable)\b", re.I), 0.8),
    ("support", re.compile(r"\b(help|issue|problem|broken|error|not working|support)\b", re.I), 0.75),
    ("product_enquiry", re.compile(r"\b(product|service|feature|do you offer|interested in)\b", re.I), 0.7),
    ("follow_up", re.compile(r"\b(following up|any update|checking in)\b", re.I), 0.7),
]

_SENT_RULES: list[tuple[str, re.Pattern[str], float]] = [
    ("urgent", re.compile(r"\b(urgent|asap|immediately|emergency|right now)\b", re.I), 0.9),
    ("negative", re.compile(r"\b(angry|terrible|awful|hate|frustrated|worst|scam)\b", re.I), 0.85),
    ("positive", re.compile(r"\b(thanks|thank you|great|awesome|perfect|love it)\b", re.I), 0.8),
]


def classify_message_rules(text: str) -> dict[str, Any]:
    body = (text or "").strip()
    if is_optout_keyword(body):
        return {
            "current_intent": "opt_out",
            "intent_confidence": 1.0,
            "current_sentiment": "neutral",
            "sentiment_confidence": 1.0,
            "source": "deterministic_opt_out",
            "classified_at": datetime.now(timezone.utc),
        }
    intent, iconf = "unknown", 0.3
    for name, pat, conf in _RULES:
        if pat.search(body):
            intent, iconf = name, conf
            break
    sentiment, sconf = "neutral", 0.5
    for name, pat, conf in _SENT_RULES:
        if pat.search(body):
            sentiment, sconf = name, conf
            break
    return {
        "current_intent": intent,
        "intent_confidence": iconf,
        "current_sentiment": sentiment,
        "sentiment_confidence": sconf,
        "source": "rules",
        "classified_at": datetime.now(timezone.utc),
    }


def should_auto_escalate(classification: dict) -> bool:
    sent = classification.get("current_sentiment")
    if sent == "urgent" and settings.AI_AUTO_ESCALATE_URGENT:
        return True
    if sent == "negative" and settings.AI_AUTO_ESCALATE_NEGATIVE:
        return True
    return False


def apply_classification_to_lead(db, *, tenant_id: str, lead_id: str, text: str) -> dict:
    from bson import ObjectId

    result = classify_message_rules(text)
    fields = {
        "current_intent": result["current_intent"],
        "intent_confidence": result["intent_confidence"],
        "current_sentiment": result["current_sentiment"],
        "sentiment_confidence": result["sentiment_confidence"],
        "classified_at": result["classified_at"],
        "updated_at": datetime.now(timezone.utc),
    }
    if should_auto_escalate(result):
        fields["needs_human"] = True
    db.leads.update_one({"_id": ObjectId(lead_id), "user_id": tenant_id}, {"$set": fields})
    return result
