"""Tenant-safe conversation context loading and token estimation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from app.config import settings
from app.services.ai_config import sanitize_text

# Messages the business started (campaigns, marketing/utility templates). A customer reply
# to one of these belongs to it however late it arrives.
BUSINESS_INITIATED_PURPOSES = frozenset({"campaign", "marketing", "transactional"})


def is_business_initiated(doc: Optional[dict]) -> bool:
    if not doc or doc.get("direction") == "inbound":
        return False
    return bool(doc.get("campaign_id") or doc.get("blast_id")) or (doc.get("message_purpose") in BUSINESS_INITIATED_PURPOSES)


def _as_utc(dt) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def current_session(raw: list[dict], gap_hours: Optional[float]) -> list[dict]:
    """Trim oldest→newest messages to the current conversation: cut at the latest gap
    longer than `gap_hours`. If the customer is replying late to a business-initiated
    message, that message is kept (it's what they're answering) and the cut is just above it."""
    if not gap_hours or float(gap_hours) <= 0 or len(raw) < 2:
        return raw
    gap = timedelta(hours=float(gap_hours))
    for i in range(len(raw) - 1, 0, -1):
        newer, older = raw[i], raw[i - 1]
        t_new, t_old = _as_utc(newer.get("created_at")), _as_utc(older.get("created_at"))
        if t_new is None or t_old is None or t_new - t_old <= gap:
            continue
        if newer.get("direction") == "inbound" and is_business_initiated(older):
            return raw[i - 1 :]
        return raw[i:]
    return raw


def estimate_tokens(text: str) -> int:
    """Approximate token count (~4 chars/token)."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _msg_text(doc: dict) -> str:
    body = (doc.get("message") or "").strip()
    if not body and doc.get("media_filename"):
        body = f"[media: {doc.get('media_content_type') or 'file'} {doc.get('media_filename')}]"
    elif not body and doc.get("media_url"):
        body = f"[media: {doc.get('media_content_type') or 'attachment'}]"
    return sanitize_text(body, max_len=2000)


def load_conversation_context(
    db,
    *,
    tenant_id: str,
    lead_id: str,
    summary: Optional[str] = None,
    max_messages: Optional[int] = None,
    max_chars: Optional[int] = None,
    session_gap_hours: Optional[float] = None,
) -> dict[str, Any]:
    """Load recent messages for AI. Tenant + lead scoped only. With `session_gap_hours`,
    only the current conversation is returned (see `current_session`)."""
    limit = max(1, min(50, int(max_messages or settings.AI_MAX_CONTEXT_MESSAGES or settings.OPENAI_MAX_HISTORY)))
    max_c = max(500, int(max_chars or settings.AI_MAX_CONTEXT_CHARS))

    cur = (
        db.messages.find(
            {
                "user_id": tenant_id,
                "lead_id": lead_id,
                "status": {"$nin": ["failed", "canceled", "cancelled"]},
                "message_purpose": {"$nin": ["opt_out_confirmation"]},
            }
        )
        .sort("created_at", -1)
        .limit(limit * 2)  # over-fetch then trim by chars
    )
    raw = current_session(list(reversed(list(cur))), session_gap_hours)

    messages: list[dict[str, Any]] = []
    chars = 0
    truncated = False
    for doc in raw:
        text = _msg_text(doc)
        if not text:
            continue
        role = "user" if doc.get("direction") == "inbound" else "assistant"
        entry = {
            "role": role,
            "content": text,
            "message_id": str(doc.get("_id")),
            "created_at": doc.get("created_at"),
        }
        add = len(text) + 16
        if messages and chars + add > max_c:
            truncated = True
            continue
        # Prefer newest: rebuild from end
        messages.append(entry)
        chars += add

    # Keep only last `limit` after char filter
    if len(messages) > limit:
        truncated = True
        messages = messages[-limit:]

    # If we over-fetched from oldest side due to char skip, rebuild from newest
    if truncated and not messages:
        for doc in reversed(raw):
            text = _msg_text(doc)
            if not text:
                continue
            role = "user" if doc.get("direction") == "inbound" else "assistant"
            messages.insert(
                0,
                {
                    "role": role,
                    "content": text,
                    "message_id": str(doc.get("_id")),
                    "created_at": doc.get("created_at"),
                },
            )
            if len(messages) >= limit:
                break

    # When older messages fell out of the window, keep the conversation's first customer message
    # available so "what did I ask first?" is answered correctly in long chats.
    first_customer_message = None
    if truncated or len(raw) >= limit * 2:
        shown = {m["message_id"] for m in messages}
        history = list(
            db.messages.find(
                {
                    "user_id": tenant_id,
                    "lead_id": lead_id,
                    "status": {"$nin": ["failed", "canceled", "cancelled"]},
                    "message_purpose": {"$nin": ["opt_out_confirmation"]},
                }
            )
            .sort("created_at", -1)
            .limit(500)
        )
        session = current_session(list(reversed(history)), session_gap_hours)
        first = next((d for d in session if d.get("direction") == "inbound" and _msg_text(d)), None)
        if first is not None and str(first.get("_id")) not in shown:
            first_customer_message = _msg_text(first)[:500]

    est = sum(estimate_tokens(m["content"]) for m in messages)
    if summary and truncated:
        est += estimate_tokens(summary)

    return {
        "messages": messages,
        "estimated_input_tokens": est,
        "truncated": truncated,
        "summary_used": bool(summary and truncated),
        "message_count": len(messages),
        "max_messages": limit,
        "max_chars": max_c,
        "summary": summary if (summary and truncated) else (summary or None),
        "first_customer_message": first_customer_message,
    }
