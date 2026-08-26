"""Inbound agent routing (Phase 1).

Decides WHICH of a tenant's active agents handles an inbound message, and makes the
choice sticky per conversation. Order of resolution:

  1. Sticky      — lead.assigned_agent_id, if still an active agent for this tenant.
  2. Single      — only one active agent → use it.
  3. Keyword     — inbound text matches an agent's routing_keywords (best score wins).
  4. LLM router  — classify against agent name/description (only when >1 candidate and
                   keywords didn't decide, and AI is available).
  5. Default     — the tenant's is_default agent (a deliberate catch-all).
  6. Generic     — no match and no default → return None → the caller replies with a
                   generic LLM (no agent). Not persisted, so a later on-topic message can
                   still route to a matching agent.

Never raises. Returns None only for the generic case (no confident match + no default
agent). Sync (pymongo) — runs inside the RQ worker.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from bson import ObjectId

logger = logging.getLogger(__name__)


def _active_agents(db, user_id: str) -> list[dict]:
    return list(
        db.agents.find({"user_id": user_id, "status": "active"}).sort("updated_at", -1)
    )


def _latest_inbound_text(db, user_id: str, lead_id: str) -> str:
    doc = db.messages.find_one(
        {"user_id": user_id, "lead_id": lead_id, "direction": "inbound"},
        sort=[("created_at", -1)],
    )
    return ((doc or {}).get("message") or "").strip()


def _keyword_pick(agents: list[dict], text: str) -> Optional[dict]:
    if not text:
        return None
    low = text.lower()
    best, best_score = None, 0
    for a in agents:
        score = 0
        for kw in a.get("routing_keywords") or []:
            k = str(kw).strip().lower()
            if not k:
                continue
            # word-boundary match when the keyword is a single token, else substring
            if re.search(r"\b" + re.escape(k) + r"\b", low) if " " not in k else k in low:
                score += 1
        if score > best_score:
            best, best_score = a, score
    return best if best_score > 0 else None


def _default_agent(agents: list[dict]) -> Optional[dict]:
    for a in agents:
        if a.get("is_default"):
            return a
    return None


def _llm_pick(agents: list[dict], text: str, *, tenant_id: str) -> Optional[dict]:
    """Ask the model which agent fits. Returns an agent or None (caller falls back)."""
    try:
        from app.services.ai_provider import chat_completion
    except Exception:
        return None
    if not text:
        return None
    roster = []
    for i, a in enumerate(agents):
        roster.append(
            f"{i}. {a.get('name') or 'Agent'} — {(a.get('description') or a.get('kind') or '').strip()[:200]}"
        )
    system = (
        "You are a router. Pick the single agent that clearly fits the customer's message. "
        "If NONE of the agents clearly covers the message's topic, reply -1 (do NOT force a "
        "loose match). Reply with ONLY the agent's number, or -1.\n\nAGENTS:\n"
        + "\n".join(roster)
    )
    try:
        result = chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text[:1000]},
            ],
            model="gpt-4o-mini",
            temperature=0.0,
            max_tokens=5,
            tenant_id=tenant_id,
            operation="agent_route",
            record_usage=False,
        )
    except Exception:
        return None
    if not getattr(result, "success", False):
        return None
    m = re.search(r"-?\d+", (result.text or ""))
    if not m:
        return None
    idx = int(m.group(0))
    if 0 <= idx < len(agents):
        return agents[idx]
    return None


def _persist(db, lead_id: str, agent: dict) -> None:
    try:
        db.leads.update_one(
            {"_id": ObjectId(lead_id)},
            {"$set": {"assigned_agent_id": str(agent["_id"])}},
        )
    except Exception:
        logger.debug("failed to persist assigned_agent_id", exc_info=True)


def select_agent_for_inbound(
    db,
    user_id: str,
    lead: dict,
    message_text: Optional[str] = None,
) -> Optional[dict]:
    """Resolve the owning agent for an inbound message and make it sticky."""
    lead_id = str(lead.get("_id"))
    agents = _active_agents(db, user_id)
    if not agents:
        return None

    by_id = {str(a["_id"]): a for a in agents}

    # 1. Sticky — keep the conversation with its agent.
    assigned = (lead.get("assigned_agent_id") or "").strip()
    if assigned and assigned in by_id:
        return by_id[assigned]

    # 2. Single active agent.
    if len(agents) == 1:
        _persist(db, lead_id, agents[0])
        return agents[0]

    text = message_text if message_text is not None else _latest_inbound_text(db, user_id, lead_id)

    # 3. Keyword routing.
    picked = _keyword_pick(agents, text)
    if picked:
        _persist(db, lead_id, picked)
        return picked

    # 4. LLM router.
    picked = _llm_pick(agents, text, tenant_id=user_id)
    if picked:
        _persist(db, lead_id, picked)
        return picked

    # 5. No confident match. If the tenant set a catch-all default agent, use it.
    default = _default_agent(agents)
    if default:
        _persist(db, lead_id, default)
        return default

    # 6. No match and no default → generic LLM reply (agent=None). Deliberately NOT
    #    persisted, so a later on-topic message can still route to a matching agent.
    return None
