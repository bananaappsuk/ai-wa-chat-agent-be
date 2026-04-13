from typing import Literal, Optional
from pydantic import BaseModel, Field


Score = Literal["hot", "warm", "cold"]


class LeadCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)
    score: Score = "cold"
    source: Optional[str] = Field(default=None, max_length=100)
    tags: list[str] = Field(default_factory=list)


class LeadUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)
    score: Optional[Score] = None
    source: Optional[str] = Field(default=None, max_length=100)
    tags: Optional[list[str]] = None
    blacklisted: Optional[bool] = None
