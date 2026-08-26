from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


Tone = Literal["sales", "support", "neutral"]
Status = Literal["active", "inactive"]
Kind = Literal["inbound", "outbound", "sales", "support"]


class SocialLinks(BaseModel):
    facebook: Optional[str] = None
    instagram: Optional[str] = None
    twitter: Optional[str] = None
    linkedin: Optional[str] = None
    tiktok: Optional[str] = None
    youtube: Optional[str] = None


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Kind = "inbound"
    prompt: Optional[str] = Field(default=None, max_length=4000)
    tone: Tone = "neutral"
    knowledge_base: Optional[str] = Field(default=None, max_length=10000)
    status: Status = "active"
    # --- Routing (Phase 1: which agent handles an inbound message) ---
    # Short domain/purpose description — shown in UI and given to the LLM router.
    description: Optional[str] = Field(default=None, max_length=500)
    # Deterministic routing: inbound whose text matches any keyword routes here first.
    routing_keywords: list[str] = Field(default_factory=list, max_length=30)
    # Fallback agent for a tenant when nothing else matches (only one may be default).
    is_default: bool = False
    # Per-agent identity override so multiple agents feel distinct (falls back to the
    # tenant-level ai_business_description when unset).
    business_description: Optional[str] = Field(default=None, max_length=2000)

    @field_validator("routing_keywords", mode="before")
    @classmethod
    def _clean_keywords(cls, v):
        if not v:
            return []
        if isinstance(v, str):
            v = [p for p in v.replace("\n", ",").split(",")]
        out: list[str] = []
        for k in v:
            s = str(k).strip().lower()[:40]
            if s and s not in out:
                out.append(s)
        return out[:30]
    # Safe default True — existing agents remain campaign-capable
    campaign_enabled: bool = True
    callback_number: Optional[str] = Field(default=None, max_length=20)
    logo_url: Optional[str] = Field(default=None, max_length=500)
    cta_text: Optional[str] = Field(default=None, max_length=120)
    cta_url: Optional[str] = Field(default=None, max_length=500)
    website_url: Optional[str] = Field(default=None, max_length=500)
    social_links: SocialLinks = Field(default_factory=SocialLinks)
    welcome_message: Optional[str] = Field(default=None, max_length=2000)
    terms_text: Optional[str] = Field(default=None, max_length=4000)
    support_email: Optional[str] = Field(default=None, max_length=200)
    business_hours: Optional[str] = Field(default=None, max_length=200)
    booking_link: Optional[str] = Field(default=None, max_length=500)
    price_floor: Optional[str] = Field(default=None, max_length=50)
    price_ceiling: Optional[str] = Field(default=None, max_length=50)


class AgentUpdate(AgentCreate):
    name: Optional[str] = Field(default=None, max_length=100)
