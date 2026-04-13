from typing import Optional
from pydantic import BaseModel, Field


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    message: Optional[str] = Field(default=None, max_length=1600)
    media_url: Optional[str] = Field(default=None, max_length=500)
    status: str = "draft"
    scheduled_at: Optional[str] = None


class CampaignUpdate(CampaignCreate):
    name: Optional[str] = Field(default=None, max_length=100)


class BlastCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=1600)
    recipients: list[str] = Field(min_length=1)
