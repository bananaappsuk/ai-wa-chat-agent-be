"""Tenant-safe conversation context loading and token estimation."""
from __future__ import annotations

from typing import Any, Optional

from app.config import settings
from app.services.ai_config import sanitize_text


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
) -> dict[str, Any]:
    """Load recent messages for AI. Tenant + lead scoped only."""
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
    raw = list(reversed(list(cur)))

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
    }
