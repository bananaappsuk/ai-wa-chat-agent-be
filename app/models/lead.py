from typing import Literal, Optional
from pydantic import BaseModel, Field, field_validator


Score = Literal["hot", "warm", "cold"]


class LeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)
    score: Score = "cold"
    source: Optional[str] = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("tags")
    @classmethod
    def limit_tag_len(cls, v: list[str]) -> list[str]:
        return [str(t)[:40] for t in (v or [])[:50]]


class LeadUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)
    score: Optional[Score] = None
    source: Optional[str] = Field(default=None, max_length=100)
    tags: Optional[list[str]] = Field(default=None, max_length=50)
    blacklisted: Optional[bool] = None

    @field_validator("tags")
    @classmethod
    def limit_tag_len(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        return [str(t)[:40] for t in v[:50]]
