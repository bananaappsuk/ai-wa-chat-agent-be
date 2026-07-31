from typing import Literal, Optional
from pydantic import BaseModel, Field


TemplateStatus = Literal["draft", "pending", "approved", "rejected"]


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    content_sid: str = Field(min_length=1, max_length=100)
    language: str = Field(default="en", max_length=20)
    status: TemplateStatus = "draft"
    variables: list[str] = Field(default_factory=list)


class TemplateUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)
    content_sid: Optional[str] = Field(default=None, max_length=100)
    language: Optional[str] = Field(default=None, max_length=20)
    status: Optional[TemplateStatus] = None
    variables: Optional[list[str]] = None
