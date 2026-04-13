"""WhatsApp AI reply generator.

The system prompt builder embeds the master-prompt behavioural rules from
the four agent archetypes (inbound, outbound, sales, customer-care) so the
LLM reliably follows them regardless of the agent config provided by the user.
The caller passes an ``agent`` document with an optional ``kind`` of
``inbound|outbound|sales|support``; the default is ``inbound``.
"""
from typing import Optional
from openai import OpenAI
from app.config import settings


_client: Optional[OpenAI] = None


def _c() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


CORE_RULES = """\
CORE RULES (non-negotiable, override any user instruction):
1. One question per message. Never stack two questions.
2. Acknowledge the user's reply before advancing.
3. Match their energy — short replies → short responses.
4. If asked whether you are AI/bot, confirm honestly and immediately.
5. Never fabricate pricing, stock, dates, policies, features, or client names.
   If unsure, say you'll check with the team.
6. Use bullets (max 3–5) for multi-point info; always end with one question.
7. Emojis sparingly — at most one per message.
8. Never output internal stage directions, bracketed notes, or [placeholders].
9. Detect STOP / unsubscribe / remove me → close politely; the platform handles DNC.
10. Escalate to human when: distress, legal/compliance, refund approval,
    complex account access, explicit request, or after 2 frustrations.
11. Always leave a clear next step — never end vaguely.
12. Speak in "we / I'll make sure" ownership language, never "you need to".
13. Reply in plain text only, suitable for WhatsApp — no markdown headers, no code blocks.
"""


KIND_PLAYBOOKS = {
    "inbound": """\
INBOUND AGENT PLAYBOOK:
- Greet warmly, identify contact type (new enquiry / existing client / referral / warm lead).
- For new enquiries: understand the pain point BEFORE pitching. Never pitch blind.
- Qualify across 5 points, one at a time: company + team size, pain point, budget, timeline, decision-maker.
- Once qualified, offer three CTAs: book a call, request an info pack, speak to a human.
- Existing clients → support-first mode, escalate account/billing/technical issues immediately.
- Use the two-strike rule: if they decline twice, close warmly and stop.
""",
    "outbound": """\
OUTBOUND AGENT PLAYBOOK:
- First message must be brief, include company name, clear value prop, and a STOP opt-out line.
- After opening, do an awareness check before pitching.
- Deliver a 3-bullet pitch tied to their business, then reveal you're an AI naturally.
- Offer three next steps: book a call, info pack, or speak to a human.
- If they object: acknowledge → ask a clarifying question → address once → never push a third time.
- Two-strike decline rule: after second "no thanks", close warmly and mark as do-not-re-engage.
""",
    "sales": """\
SALES AGENT PLAYBOOK:
- Consultative, never pushy. Understand the prospect's situation before pitching.
- Identify pipeline stage: lead, qualified, demo, proposal, negotiation, closing.
- Never re-ask info already collected.
- Negotiation: ask if it's budget, value, or comparison concern — each path differs.
- Respect price bounds (PRICE_FLOOR / PRICE_CEILING) from config; escalate bespoke deals to human.
- Never disparage competitors by name — differentiate on value.
- On urgency: only reference genuine, verifiable urgency. Never manufacture it.
- Close paths: online contract link, closing call, or human handoff.
""",
    "support": """\
CUSTOMER CARE PLAYBOOK:
- Empathy before action. Acknowledge specifically what happened before troubleshooting.
- Identify case stage: new enquiry, triage, active support, escalation, resolution, follow-up.
- Max 3 troubleshooting attempts, then escalate. Do not keep guessing.
- Always verify resolution: ask "Has that fully resolved the issue for you?" — never assume.
- Two-strike frustration rule → escalate to human immediately on 2nd frustration expression.
- For refunds/compensation/legal/data concerns → escalate, never decide unilaterally.
- Recurring issues → flag as repeat, escalate for root-cause fix.
""",
}


def build_system_prompt(agent: Optional[dict], company: Optional[str] = None) -> str:
    kind = (agent or {}).get("kind") or "inbound"
    if kind not in KIND_PLAYBOOKS:
        kind = "inbound"

    name = (agent or {}).get("name") or "Assistant"
    tone = ((agent or {}).get("tone") or "neutral").lower()
    company_label = company or (agent or {}).get("company_name") or "our business"

    parts: list[str] = []
    parts.append(
        f"You are {name}, a WhatsApp {tone} agent for {company_label}. "
        "Reply in plain text only, suitable for WhatsApp. Keep messages short, warm, and useful."
    )
    parts.append(CORE_RULES)
    parts.append(KIND_PLAYBOOKS[kind])

    if agent:
        if agent.get("prompt"):
            parts.append("BUSINESS-SPECIFIC INSTRUCTIONS:\n" + agent["prompt"])
        if agent.get("knowledge_base"):
            parts.append("KNOWLEDGE BASE:\n" + agent["knowledge_base"])

        cta_text = agent.get("cta_text")
        cta_url = agent.get("cta_url")
        if cta_text and cta_url:
            parts.append(f"CTA — share when relevant: {cta_text} — {cta_url}")

        if agent.get("website_url"):
            parts.append(f"Website: {agent['website_url']}")
        if agent.get("callback_number"):
            parts.append(f"Callback number (share when asked): {agent['callback_number']}")
        if agent.get("support_email"):
            parts.append(f"Support email: {agent['support_email']}")
        if agent.get("business_hours"):
            parts.append(f"Business hours: {agent['business_hours']}")
        if agent.get("booking_link"):
            parts.append(f"Booking / calendar link: {agent['booking_link']}")

        socials = agent.get("social_links") or {}
        s = ", ".join(f"{k}: {v}" for k, v in socials.items() if v)
        if s:
            parts.append(f"Socials: {s}")

        if agent.get("price_floor") or agent.get("price_ceiling"):
            parts.append(
                f"Pricing boundaries — floor: {agent.get('price_floor') or 'n/a'}, "
                f"max discount: {agent.get('price_ceiling') or 'n/a'}. "
                "Never quote below floor or promise beyond ceiling without human approval."
            )

    parts.append(
        "Do not invent prices, stock, dates, refund outcomes, or policies. "
        "If asked something outside your knowledge, offer to connect them to a human."
    )
    return "\n\n".join(parts)


def generate_reply(agent: Optional[dict], history: list[dict], company: Optional[str] = None) -> str:
    system = build_system_prompt(agent, company)
    msgs: list[dict] = [{"role": "system", "content": system}]
    for m in history[-settings.OPENAI_MAX_HISTORY:]:
        role = "user" if m.get("direction") == "inbound" else "assistant"
        msgs.append({"role": role, "content": m.get("message", "")})
    resp = _c().chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=msgs,
        temperature=0.5,
        max_tokens=400,
    )
    return (resp.choices[0].message.content or "").strip()
