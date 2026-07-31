"""Inbound/outbound moderation and prompt-injection heuristics."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.config import settings

_INJECTION = re.compile(
    r"(ignore (all |previous |prior )?instructions|reveal (your |the )?system prompt|"
    r"disregard (the )?rules|you are now|jailbreak|dan mode|act as developer|"
    r"print your (system|hidden) prompt)",
    re.I,
)
_THREAT = re.compile(r"\b(kill you|bomb|i will hurt|shoot you|murder)\b", re.I)
_SELF_HARM = re.compile(r"\b(kill myself|suicide|end my life|self[- ]harm)\b", re.I)
_ILLEGAL = re.compile(r"\b(buy drugs|hire a hitman|credit card fraud|child porn)\b", re.I)
_SEXUAL = re.compile(r"\b(send nudes|explicit sex with|pornographic)\b", re.I)
_HARASS = re.compile(r"\b(you (are|re) (stupid|idiot|worthless)|go die)\b", re.I)
_SPAM = re.compile(r"(https?://\S+\s+){4,}|(.{8,})\1{4,}", re.I)

_SECRET_LEAK = re.compile(
    r"(sk-[A-Za-z0-9]{20,}|api[_-]?key\s*[:=]|JWT_SECRET|BEGIN (RSA )?PRIVATE KEY|"
    r"system prompt:|CORE RULES \(non-negotiable)",
    re.I,
)
_UNSUPPORTED_PROMISE = re.compile(
    r"\b(guaranteed (refund|return|approval)|100% guaranteed|we will definitely give you money)\b",
    re.I,
)


@dataclass
class ModerationResult:
    allowed: bool
    categories: list[str]
    escalate: bool
    reason: Optional[str] = None


def _blocked_set() -> set[str]:
    return {c.strip().lower() for c in (settings.AI_BLOCKED_CATEGORIES or "").split(",") if c.strip()}


def _escalation_set() -> set[str]:
    return {
        c.strip().lower()
        for c in (settings.AI_MODERATION_ESCALATION_CATEGORIES or "").split(",")
        if c.strip()
    }


def moderate_inbound(text: str) -> ModerationResult:
    body = text or ""
    cats: list[str] = []
    if _INJECTION.search(body):
        cats.append("prompt_injection")
    if _THREAT.search(body):
        cats.append("threats")
    if _SELF_HARM.search(body):
        cats.append("self_harm")
    if _ILLEGAL.search(body):
        cats.append("illegal")
    if _SEXUAL.search(body):
        cats.append("sexual")
    if _HARASS.search(body):
        cats.append("harassment")
    if _SPAM.search(body):
        cats.append("spam")

    blocked = _blocked_set()
    hit = [c for c in cats if c in blocked]
    escalate = any(c in _escalation_set() for c in cats)
    # Complaints ("terrible service") are not blocked
    if not hit:
        return ModerationResult(allowed=True, categories=cats, escalate=escalate)
    # prompt_injection is tracked but conversation continues with untrusted delimiting
    if hit == ["prompt_injection"]:
        return ModerationResult(allowed=True, categories=cats, escalate=False, reason="prompt_injection")
    return ModerationResult(
        allowed=False,
        categories=cats,
        escalate=escalate or True,
        reason=hit[0],
    )


def moderate_outbound(text: str, *, disallowed_topics: str = "") -> ModerationResult:
    body = text or ""
    cats: list[str] = []
    if _SECRET_LEAK.search(body):
        cats.append("data_leak")
    if _UNSUPPORTED_PROMISE.search(body):
        cats.append("unsupported_promises")
    if _INJECTION.search(body) and "system prompt" in body.lower():
        cats.append("prompt_leak")
    topics = [t.strip().lower() for t in (disallowed_topics or "").split(",") if t.strip()]
    low = body.lower()
    for t in topics:
        if t and t in low:
            cats.append("disallowed_topic")
            break
    if cats:
        return ModerationResult(allowed=False, categories=cats, escalate=False, reason=cats[0])
    return ModerationResult(allowed=True, categories=[], escalate=False)
