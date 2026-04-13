from typing import Optional
from openai import OpenAI
from app.config import settings


_client: Optional[OpenAI] = None


def _c() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=settings.OPENAI_API_KEY)
    return _client


def build_system_prompt(agent: Optional[dict], company: Optional[str] = None) -> str:
    if not agent:
        return (
            "You are a helpful WhatsApp sales and support assistant. "
            "Be concise, friendly, and professional. Reply in plain text suitable for WhatsApp."
        )
    parts: list[str] = []
    tone = (agent.get("tone") or "neutral").lower()
    name = agent.get("name") or "Assistant"
    parts.append(
        f"You are {name}, a WhatsApp {tone} agent for {company or 'our business'}. "
        "Reply in plain text only, suitable for WhatsApp. Keep messages short, warm, and useful."
    )
    if agent.get("prompt"):
        parts.append(agent["prompt"])
    if agent.get("knowledge_base"):
        parts.append("Knowledge base:\n" + agent["knowledge_base"])
    cta_text = agent.get("cta_text")
    cta_url = agent.get("cta_url")
    if cta_text and cta_url:
        parts.append(f"When relevant, share the CTA: {cta_text} — {cta_url}")
    if agent.get("website_url"):
        parts.append(f"Website: {agent['website_url']}")
    if agent.get("callback_number"):
        parts.append(f"Callback number to share when asked: {agent['callback_number']}")
    socials = agent.get("social_links") or {}
    socials_str = ", ".join(f"{k}: {v}" for k, v in socials.items() if v)
    if socials_str:
        parts.append(f"Socials: {socials_str}")
    parts.append(
        "Never invent prices, stock, or promises. If unsure, offer to connect the customer to a human."
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
