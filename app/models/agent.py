from typing import Literal, Optional
from pydantic import BaseModel, Field


Tone = Literal["sales", "support", "neutral"]
Status = Literal["active", "inactive"]


class SocialLinks(BaseModel):
    facebook: Optional[str] = None
    instagram: Optional[str] = None
    twitter: Optional[str] = None
    linkedin: Optional[str] = None
    tiktok: Optional[str] = None
    youtube: Optional[str] = None


class AgentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    prompt: Optional[str] = Field(default=None, max_length=4000)
    tone: Tone = "neutral"
    knowledge_base: Optional[str] = Field(default=None, max_length=10000)
    status: Status = "active"
    callback_number: Optional[str] = Field(default=None, max_length=20)
    logo_url: Optional[str] = Field(default=None, max_length=500)
    cta_text: Optional[str] = Field(default=None, max_length=120)
    cta_url: Optional[str] = Field(default=None, max_length=500)
    website_url: Optional[str] = Field(default=None, max_length=500)
    social_links: SocialLinks = Field(default_factory=SocialLinks)
    welcome_message: Optional[str] = Field(default=None, max_length=2000)
    terms_text: Optional[str] = Field(default=None, max_length=4000)


class AgentUpdate(AgentCreate):
    name: Optional[str] = Field(default=None, max_length=100)
