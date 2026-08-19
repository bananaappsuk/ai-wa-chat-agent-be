from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


TemplateStatus = Literal["draft", "pending", "approved", "rejected"]
TemplateProvider = Literal["twilio_content", "meta"]


class TemplateCreate(BaseModel):
    """Twilio Content (HX) create payload. Meta templates are synced, not created here."""

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


class TemplateMetaFields(BaseModel):
    """Optional Meta Cloud API fields stored on the same templates collection (additive)."""

    provider: TemplateProvider = "twilio_content"
    meta_template_name: Optional[str] = None
    meta_language_code: Optional[str] = None
    meta_graph_id: Optional[str] = None
    components: Optional[list[dict[str, Any]]] = None
    variable_schema: Optional[list[dict[str, Any]]] = None
    whatsapp_approval_status_raw: Optional[str] = None
