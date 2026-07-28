"""AI response quality validation and WhatsApp post-processing."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from app.config import settings
from app.services.ai_moderation import moderate_outbound

_URL = re.compile(r"https?://[^\s]+", re.I)
_SECRETISH = re.compile(r"(sk-[A-Za-z0-9]{16,}|api[_-]?key\s*[:=]|BEGIN PRIVATE KEY)", re.I)
_PROMPT_LEAK = re.compile(r"(CORE RULES|SYSTEM PROMPT|<<<USER_MESSAGE>>>|TENANT CUSTOM INSTRUCTIONS)", re.I)
_TEMPLATE_LEAK = re.compile(r"\{\{[^{}]+\}\}|\[\[[^\[\]]+\]\]")
_PRICE = re.compile(r"(?:£|\$|€)\s?(\d+(?:\.\d{1,2})?)")


@dataclass
class QualityResult:
    ok: bool
    text: str
    reason: Optional[str] = None
    low_confidence: bool = False


def post_process(text: str) -> str:
    t = (text or "").strip()
    t = t.replace("**", "").replace("__", "")
    t = re.sub(r"^#+\s*", "", t, flags=re.M)
    t = re.sub(r"\|.+\|", "", t)  # strip markdown tables roughly
    t = re.sub(r"\n{3,}", "\n\n", t)
    max_c = int(settings.AI_MAX_RESPONSE_CHARS or 1200)
    if len(t) > max_c:
        t = t[: max_c - 1].rsplit(" ", 1)[0] + "…"
    return t.strip()


def validate_output(
    text: str,
    *,
    disallowed_topics: str = "",
    price_floor: Optional[float] = None,
    price_ceiling: Optional[float] = None,
    allowed_url_hosts: Optional[set[str]] = None,
) -> QualityResult:
    cleaned = post_process(text)
    if not cleaned:
        return QualityResult(ok=False, text="", reason="empty")
    if len(cleaned) > int(settings.AI_MAX_RESPONSE_CHARS or 1200) + 50:
        return QualityResult(ok=False, text=cleaned, reason="too_long")
    if _SECRETISH.search(cleaned) or _PROMPT_LEAK.search(cleaned):
        return QualityResult(ok=False, text="", reason="secret_or_prompt_leak")
    if _TEMPLATE_LEAK.search(cleaned):
        return QualityResult(ok=False, text=cleaned, reason="template_leak")
    # repetition
    words = cleaned.split()
    if len(words) > 12 and len(set(words)) < max(3, len(words) // 8):
        return QualityResult(ok=False, text=cleaned, reason="excessive_repetition")

    mod = moderate_outbound(cleaned, disallowed_topics=disallowed_topics)
    if not mod.allowed:
        return QualityResult(ok=False, text="", reason=mod.reason or "moderation")

    if allowed_url_hosts is not None:
        for m in _URL.finditer(cleaned):
            host = m.group(0).split("/")[2].lower() if "://" in m.group(0) else ""
            if host and host not in allowed_url_hosts:
                return QualityResult(ok=False, text=cleaned, reason="unsupported_url")

    if price_floor is not None or price_ceiling is not None:
        for m in _PRICE.finditer(cleaned):
            try:
                val = float(m.group(1))
            except ValueError:
                continue
            if price_floor is not None and val < float(price_floor):
                return QualityResult(ok=False, text=cleaned, reason="price_below_floor")
            if price_ceiling is not None and val > float(price_ceiling):
                return QualityResult(ok=False, text=cleaned, reason="price_above_ceiling")

    return QualityResult(ok=True, text=cleaned)
