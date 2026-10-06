"""Inbound message routing — decided fresh for EVERY message, using the conversation.

There are no locks. For each inbound message a small routing model reads the recent
conversation (who said what, including which agent answered) plus the tenant's agents, and
decides who should reply to the LATEST message:

  * general  — greetings, how-are-you, thanks, small talk, goodbyes, and anything that isn't
               one of the agents' topics → a friendly general assistant replies (no agent pitch),
               even in the middle of a conversation with an agent;
  * agent i  — a follow-up on the topic an agent has been handling ("how long is it?", "yes
               please", answering its question) stays with that agent; a new topic another
               agent covers goes to that agent.

The same call also writes the standalone search query used for knowledge retrieval, and —
when the message needs current real-world information (weather, news, scores, exchange
rates…) — a web search query for a live lookup, so routing adds no extra model call.

Overrides and fallbacks:
  * A manual "Handled by" pin (set by a human in Live Chat / Leads) always wins.
  * If the tenant marked a default agent, "general" goes to it instead of the general assistant.
  * If the routing model is unavailable: keywords → continue with the agent that answered the
    previous turn (unless the message is small talk) → default agent → general.

`lead.assigned_agent_id` is only a record of who answered last (shown in the UI); it never
locks routing. Never raises. Sync (pymongo) — runs inside the RQ worker.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from bson import ObjectId

from app.config import settings
from app.services.ai_context import current_session

logger = logging.getLogger(__name__)

_CONTEXT_TURNS = 10


@dataclass
class RouteDecision:
    agent: Optional[dict]
    mode: str  # agent | general | pinned | default | none
    query: str = ""
    continues: bool = False
    via: str = "llm"  # llm | fallback | pin | no_agents
    topics: list[str] = field(default_factory=list)
    lookup_query: str = ""  # set when the reply needs a live web lookup


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


def _keyword_score(agent: dict, text: str) -> int:
    low = (text or "").lower()
    score = 0
    for kw in agent.get("routing_keywords") or []:
        k = str(kw).strip().lower()
        if not k:
            continue
        # word-boundary match when the keyword is a single token, else substring
        if re.search(r"\b" + re.escape(k) + r"\b", low) if " " not in k else k in low:
            score += 1
    return score


def _keyword_pick(agents: list[dict], text: str) -> Optional[dict]:
    if not text:
        return None
    best, best_score = None, 0
    for a in agents:
        score = _keyword_score(a, text)
        if score > best_score:
            best, best_score = a, score
    return best if best_score > 0 else None


def _default_agent(agents: list[dict]) -> Optional[dict]:
    for a in agents:
        if a.get("is_default"):
            return a
    return None


def _persist(db, lead_id: str, agent: dict) -> None:
    """Record who answered last (UI only — not a lock)."""
    try:
        db.leads.update_one(
            {"_id": ObjectId(lead_id), "assigned_agent_source": {"$ne": "manual"}},
            {"$set": {"assigned_agent_id": str(agent["_id"]), "assigned_agent_source": "auto"}},
        )
    except Exception:
        logger.debug("failed to record last agent", exc_info=True)


def _topic(agent: dict) -> str:
    return (agent.get("description") or agent.get("name") or "").strip()


def conversation_turns(db, user_id: str, lead_id: str, limit: int = _CONTEXT_TURNS) -> list[dict]:
    """Recent turns of the current conversation, oldest first, labelled with who spoke.
    The last item is the customer's latest message."""
    docs = list(
        db.messages.find(
            {
                "user_id": user_id,
                "lead_id": lead_id,
                "status": {"$nin": ["failed", "canceled", "cancelled"]},
                "message_purpose": {"$nin": ["opt_out_confirmation"]},
            }
        )
        .sort("created_at", -1)
        .limit(limit * 2)
    )
    docs = current_session(list(reversed(docs)), settings.AI_CONTEXT_SESSION_GAP_HOURS)[-limit:]
    campaign_agents: dict[str, str] = {}
    turns: list[dict] = []
    for d in docs:
        text = (d.get("message") or "").strip()
        if not text:
            continue
        if d.get("direction") == "inbound":
            turns.append({"who": "Customer", "text": text})
            continue
        name = (d.get("agent_name") or "").strip()
        if not name and d.get("campaign_id"):
            cid = str(d["campaign_id"])
            if cid not in campaign_agents:
                camp = db.campaigns.find_one({"_id": ObjectId(cid)}) if ObjectId.is_valid(cid) else None
                aid = str((camp or {}).get("agent_id") or "")
                ag = db.agents.find_one({"_id": ObjectId(aid)}) if ObjectId.is_valid(aid) else None
                campaign_agents[cid] = (ag or {}).get("name") or ""
            name = campaign_agents[cid]
            label = f"Campaign message from {name}" if name else "Campaign message"
        elif d.get("blast_id"):
            label = "Broadcast message from the business"
        else:
            label = name or ("Business" if d.get("sender_type") == "human" else "General assistant")
        turns.append({"who": label, "text": text})
    return turns


_ROUTER_SYSTEM = """\
You route WhatsApp messages for a business inbox that has specialist agents. Read the \
conversation and decide who should reply to the customer's LATEST message.

AGENTS:
{roster}

RULES:
- "general" ONLY for greetings, how-are-you, small talk, thanks, goodbyes, reactions, and questions \
that are not about any agent's business (general knowledge, chit-chat). A greeting or small talk is \
"general" even in the middle of a conversation with an agent.
- Any question about a product, service, course, price, duration, schedule, booking, menu, funding or \
anything else an agent's business covers goes to THAT agent — never to "general". Use the conversation \
to resolve "it", "that", "the course".
- If the latest message continues the topic an agent has been handling — a follow-up such as \
"how long is it?", "and the price?", "yes please", "tell me more", or an answer to that agent's \
question — choose that agent.
- If the latest message raises a topic an agent covers, choose that agent, even if a different \
agent was handling the conversation before — including messages that start "also…", "and…", "btw…".
- "Do you have/offer a course or service on it?" — resolve "it" from the conversation and choose the \
agent whose description covers that subject.
- A bare "?", "and?", "more?" or similar right after an agent's answer continues with that agent.
- If no agent clearly fits, choose "general". Never force a loose match.
- Live information (weather, news, sports, exchange rates, today's events…) that isn't about an \
agent's business is "general".

Return JSON only:
{{"route": "general" or "agent", "agent_name": "<the agent's quoted name, copied exactly, or null>", \
"about_business": true if the latest message asks about a business's products, services, offers, prices, \
availability or bookings (false for greetings, small talk, reactions and general-knowledge questions), \
"continues_previous": true or false, \
"search_query": "<for an agent: the latest message rewritten as one standalone English search query \
that names the business or product it is about and resolves 'it'/'that' from the conversation, e.g. \
'AI Agent Hub government funding for small businesses'; for general: empty>", \
"live_lookup": "<only if a good answer needs CURRENT real-world information that changes over time — \
weather, news, sports results or fixtures, exchange rates, stock or crypto prices, travel or traffic \
status, today's events, a public place's opening hours, or recent facts — one short web search query \
with the place and time, resolving them from the conversation, e.g. 'weather in Dagenham today'. \
Empty for everything else: small talk, timeless general knowledge, and anything about the agents' \
businesses (their own knowledge covers that).>"}}"""


def _llm_route(agents: list[dict], turns: list[dict], *, tenant_id: str) -> Optional[dict]:
    try:
        from app.services.ai_provider import chat_completion
    except Exception:
        return None
    roster = "\n".join(
        f"- \"{a.get('name') or 'Agent'}\": {_topic(a)[:220]}"
        + (f" (keywords: {', '.join(a.get('routing_keywords')[:12])})" if a.get("routing_keywords") else "")
        for a in agents
    ) or "(none — every message is \"general\")"
    convo = "\n".join(f"{t['who']}: {t['text'][:400]}" for t in turns[:-1]) or "(no earlier messages)"
    latest = turns[-1]["text"][:1000] if turns else ""
    hints = [a.get("name") for a in agents if _keyword_score(a, latest) > 0]
    hint_line = (
        f"\n\nKEYWORD MATCH: the latest message contains keywords of {', '.join(repr(h) for h in hints)}. "
        "Route to that agent unless the message is purely a general-knowledge question (e.g. 'what is "
        "machine learning?'), small talk, or about several businesses at once." if hints else ""
    )
    try:
        res = chat_completion(
            messages=[
                {"role": "system", "content": _ROUTER_SYSTEM.format(roster=roster)},
                {"role": "user", "content": f"CONVERSATION (oldest first):\n{convo}\n\nLATEST CUSTOMER MESSAGE:\n{latest}{hint_line}"},
            ],
            model=settings.AI_ROUTER_MODEL or "gpt-4o-mini",
            temperature=0.0,
            max_tokens=220,
            tenant_id=tenant_id,
            operation="agent_route",
            response_format={"type": "json_object"},
            timeout=8,
            retries=1,
        )
    except Exception:
        return None
    if not getattr(res, "success", False):
        return None
    try:
        data = json.loads(res.text or "{}")
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _single_keyword_agent(agents: list[dict], turns: list[dict]) -> Optional[dict]:
    customer = [t["text"] for t in turns if t["who"] == "Customer"]
    for text in reversed(customer[-2:]):
        matched = [a for a in agents if _keyword_score(a, text) > 0]
        if matched:
            return matched[0] if len(matched) == 1 else None
    return None


def _match_agent(agents: list[dict], data: dict) -> Optional[dict]:
    """Map the router's answer to an agent by NAME (models are unreliable with list numbers):
    exact → case-insensitive → unique partial match → legacy numeric index."""
    name = str(data.get("agent_name") or data.get("agent") or "").strip()
    if name:
        for a in agents:
            if (a.get("name") or "") == name:
                return a
        low = name.lower()
        for a in agents:
            if (a.get("name") or "").strip().lower() == low:
                return a
        partial = [a for a in agents if low in (a.get("name") or "").lower() or (a.get("name") or "").lower() in low]
        if len(partial) == 1:
            return partial[0]
        # Models sometimes name an agent by what it does ("AI Course Agent" for an agent called
        # "Agent_2" described as "AI course — enrolment…"). First the phrase itself, then words.
        phrase = re.sub(r"\b(agent|assistant|bot)\b", " ", low)
        phrase = re.sub(r"\s+", " ", phrase).strip(" -—:")
        if len(phrase) >= 3:
            hits = [a for a in agents if phrase in f"{a.get('name') or ''} {_topic(a)}".lower()]
            if len(hits) == 1:
                return hits[0]
        words = set(re.findall(r"[a-z0-9]{3,}", low)) - {"agent", "the", "and", "for"}
        scored = []
        for a in agents:
            desc = set(re.findall(r"[a-z0-9]{3,}", f"{a.get('name') or ''} {_topic(a)}".lower()))
            scored.append((len(words & desc), a))
        scored.sort(key=lambda x: x[0], reverse=True)
        if scored and scored[0][0] >= 1 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
            return scored[0][1]
    try:
        idx = int(data.get("agent"))
    except (TypeError, ValueError):
        return None
    return agents[idx] if 0 <= idx < len(agents) else None


def _fallback(agents: list[dict], turns: list[dict], text: str) -> tuple[Optional[dict], bool]:
    """Routing model unavailable: keywords → previous agent (unless small talk) → general."""
    from app.services.kb.retrieve import is_small_talk

    picked = _keyword_pick(agents, text)
    if picked:
        return picked, False
    if is_small_talk(text):
        return None, False
    by_name = {(a.get("name") or "").strip(): a for a in agents}
    for t in reversed(turns[:-1]):
        if t["who"] == "Customer":
            continue
        name = t["who"].removeprefix("Campaign message from ").strip()
        return by_name.get(name), name in by_name
    return None, False


def route_message(db, user_id: str, lead: dict, message_text: Optional[str] = None) -> RouteDecision:
    """Decide who replies to the lead's latest inbound message."""
    lead_id = str(lead.get("_id"))
    agents = _active_agents(db, user_id)
    topics = [t for t in (_topic(a) for a in agents) if t]

    turns = conversation_turns(db, user_id, lead_id)
    text = (message_text if message_text is not None else (turns[-1]["text"] if turns else "")) or _latest_inbound_text(db, user_id, lead_id)
    if not turns or turns[-1]["who"] != "Customer" or (message_text is not None and turns[-1]["text"] != text):
        turns.append({"who": "Customer", "text": text})

    # The routing call runs on every path — even when the agent is fixed — because it also
    # decides whether the message needs a live lookup.
    data = _llm_route(agents, turns, tenant_id=user_id)
    lookup = str((data or {}).get("live_lookup") or "").strip()[:300]

    if not agents:
        return RouteDecision(agent=None, mode="none", via="no_agents", lookup_query=lookup)

    # A human's explicit pin always wins.
    pinned_id = str(lead.get("assigned_agent_id") or "").strip()
    if lead.get("assigned_agent_source") == "manual":
        pinned = next((a for a in agents if str(a["_id"]) == pinned_id), None)
        if pinned:
            same = data is not None and _match_agent(agents, data) is pinned
            query = str(data.get("search_query") or "").strip()[:500] if same else ""
            return RouteDecision(agent=pinned, mode="pinned", query=query, via="pin", topics=topics, lookup_query=lookup)

    agent: Optional[dict] = None
    continues = False
    query = ""
    via = "llm"
    if data is not None:
        continues = bool(data.get("continues_previous"))
        query = str(data.get("search_query") or "").strip()[:500]
        if str(data.get("route") or "").lower() == "agent":
            agent = _match_agent(agents, data)
        elif data.get("about_business"):
            # The model says it's a business question but didn't pick an agent: if exactly one
            # agent's keywords match (this message, or the previous one for "it"-style follow-ups),
            # that agent should answer rather than the general assistant.
            agent = _single_keyword_agent(agents, turns)
            if agent is not None:
                via = "llm+keywords"
    else:
        via = "fallback"
        agent, continues = _fallback(agents, turns, text)

    if agent is not None:
        _persist(db, lead_id, agent)
        return RouteDecision(agent=agent, mode="agent", query=query or text, continues=continues, via=via, topics=topics, lookup_query=lookup)

    default = _default_agent(agents)
    if default:
        _persist(db, lead_id, default)
        return RouteDecision(agent=default, mode="default", query=text, via=via, topics=topics, lookup_query=lookup)
    return RouteDecision(agent=None, mode="general", via=via, topics=topics, lookup_query=lookup)


def welcome_agent(db, user_id: str, lead: dict) -> Optional[dict]:
    """Whose welcome / T&C message a brand-new contact gets: the pinned agent, else the
    tenant's default agent, else the only agent, else whoever the first message routes to."""
    agents = _active_agents(db, user_id)
    if not agents:
        return None
    if lead.get("assigned_agent_source") == "manual":
        pinned = next((a for a in agents if str(a["_id"]) == str(lead.get("assigned_agent_id") or "")), None)
        if pinned:
            return pinned
    default = _default_agent(agents)
    if default:
        return default
    if len(agents) == 1:
        return agents[0]
    return route_message(db, user_id, lead).agent


def select_agent_for_inbound(
    db,
    user_id: str,
    lead: dict,
    message_text: Optional[str] = None,
) -> Optional[dict]:
    """The agent that should reply to the latest message (None = general assistant)."""
    return route_message(db, user_id, lead, message_text=message_text).agent
