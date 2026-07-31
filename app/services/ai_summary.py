"""Conversation summary persistence and incremental refresh."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from bson import ObjectId

from app.config import settings
from app.services.ai_config import resolve_ai_settings
from app.services.ai_context import load_conversation_context
from app.services.ai_prompt import build_chat_messages
from app.services.ai_provider import chat_completion


def get_summary(db, *, tenant_id: str, lead_id: str) -> Optional[dict]:
    return db.conversation_summaries.find_one(
        {"tenant_id": tenant_id, "conversation_id": lead_id},
        sort=[("summary_version", -1)],
    )


def maybe_enqueue_summary(tenant_id: str, lead_id: str, message_count: int) -> None:
    trigger = int(settings.AI_SUMMARY_TRIGGER_MESSAGE_COUNT or 8)
    refresh = int(settings.AI_SUMMARY_REFRESH_INTERVAL_MESSAGES or 6)
    if message_count < trigger:
        return
    if message_count != trigger and (message_count - trigger) % max(1, refresh) != 0:
        return
    try:
        from app.workers.queue import enqueue
        from app.workers import ai_tasks

        enqueue(ai_tasks.refresh_conversation_summary, tenant_id, lead_id, queue="bulk")
    except Exception:
        pass


def refresh_summary_sync(db, *, tenant_id: str, lead_id: str, force: bool = False) -> Optional[dict]:
    user = db.users.find_one({"_id": ObjectId(tenant_id)})
    ai = resolve_ai_settings(user)
    if not ai["enabled"] or not ai["summaries_enabled"]:
        return None

    existing = get_summary(db, tenant_id=tenant_id, lead_id=lead_id)
    msg_count = db.messages.count_documents(
        {"user_id": tenant_id, "lead_id": lead_id, "status": {"$nin": ["failed", "canceled"]}}
    )
    if not force and existing and int(existing.get("message_count_covered") or 0) >= msg_count:
        return existing

    ctx = load_conversation_context(
        db,
        tenant_id=tenant_id,
        lead_id=lead_id,
        summary=(existing or {}).get("summary"),
    )
    if not ctx["messages"]:
        return existing

    system = (
        "Summarise this WhatsApp conversation for a human agent. "
        "Be concise (max 120 words). Include customer goal, key facts, open questions, "
        "and next step. Do not invent details. Do not include secrets or full phone numbers."
    )
    messages = build_chat_messages(system=system, context_messages=ctx["messages"])
    result = chat_completion(
        messages=messages,
        model=ai["model"],
        fallback_model=ai["fallback_model"],
        temperature=0.2,
        max_tokens=int(settings.AI_SUMMARY_MAX_OUTPUT_TOKENS or 250),
        tenant_id=tenant_id,
        operation="summary",
        conversation_id=lead_id,
    )
    if not result.success or not result.text:
        return existing

    now = datetime.now(timezone.utc)
    version = int((existing or {}).get("summary_version") or 0) + 1
    # Prevent stale overwrite
    if existing and int(existing.get("summary_version") or 0) >= version:
        return existing

    last_mid = ctx["messages"][-1].get("message_id") if ctx["messages"] else None
    doc = {
        "tenant_id": tenant_id,
        "conversation_id": lead_id,
        "lead_id": lead_id,
        "summary": result.text[:2000],
        "summary_version": version,
        "messages_covered_until": last_mid,
        "message_count_covered": msg_count,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "updated_at": now,
        "created_at": (existing or {}).get("created_at") or now,
    }
    db.conversation_summaries.update_one(
        {"tenant_id": tenant_id, "conversation_id": lead_id},
        {"$set": doc},
        upsert=True,
    )
    # Only accept if we still own the newest version
    fresh = get_summary(db, tenant_id=tenant_id, lead_id=lead_id)
    if fresh and int(fresh.get("summary_version") or 0) > version:
        return fresh
    return doc
