import re
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


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


TemplateButtonType = Literal["URL", "PHONE_NUMBER", "QUICK_REPLY"]
_VAR = re.compile(r"\{\{\s*(\d+)\s*\}\}")


class TemplateButton(BaseModel):
    type: TemplateButtonType
    title: str = Field(min_length=1, max_length=25)
    url: Optional[str] = Field(default=None, max_length=2000)
    phone: Optional[str] = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def _check(self) -> "TemplateButton":
        if self.type in ("URL", "PHONE_NUMBER") and len(self.title) > 20:
            raise ValueError(f"'{self.title}': website and call button text can be at most 20 characters")
        if self.type == "URL" and not (self.url or "").startswith("https://"):
            raise ValueError(f"'{self.title}': the website link must start with https://")
        if self.type == "PHONE_NUMBER" and not re.fullmatch(r"\+\d{8,15}", self.phone or ""):
            raise ValueError(f"'{self.title}': the phone number must be in international format, e.g. +447453289655")
        if _VAR.search(self.title) and self.type != "QUICK_REPLY":
            raise ValueError(f"'{self.title}': button text can't contain variables")
        return self


class WhatsAppTemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    template_name: Optional[str] = Field(default=None, max_length=512)
    language: str = Field(default="en", pattern=r"^[a-z]{2}(_[A-Z]{2})?$")
    category: Literal["MARKETING", "UTILITY"] = "MARKETING"
    header_text: Optional[str] = Field(default=None, max_length=60)
    body: str = Field(min_length=1, max_length=1024)
    footer: Optional[str] = Field(default=None, max_length=60)
    buttons: list[TemplateButton] = Field(default_factory=list, max_length=13)
    samples: dict[str, str] = Field(default_factory=dict)

    @field_validator("template_name")
    @classmethod
    def _slug(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        if not re.fullmatch(r"[a-z0-9_]+", v.strip()):
            raise ValueError("Template name can only use lowercase letters, numbers and underscores")
        return v.strip()

    @model_validator(mode="after")
    def _check(self) -> "WhatsAppTemplateCreate":
        if self.footer and _VAR.search(self.footer):
            raise ValueError("The footer can't contain variables")
        used = sorted({int(n) for n in _VAR.findall((self.header_text or "") + "\n" + self.body)})
        if used and used != list(range(1, len(used) + 1)):
            raise ValueError("Variables must be numbered in order: {{1}}, {{2}}, {{3}}…")
        missing = [str(n) for n in used if not (self.samples.get(str(n)) or "").strip()]
        if missing:
            raise ValueError("Add a sample value for " + ", ".join("{{" + m + "}}" for m in missing) + " — WhatsApp needs it to approve the template")
        counts = {t: sum(1 for b in self.buttons if b.type == t) for t in ("URL", "PHONE_NUMBER", "QUICK_REPLY")}
        if counts["URL"] > 2:
            raise ValueError("At most 2 website buttons")
        if counts["PHONE_NUMBER"] > 1:
            raise ValueError("At most 1 call button")
        if counts["QUICK_REPLY"] > 10:
            raise ValueError("At most 10 quick-reply buttons")
        self.samples = {str(n): self.samples[str(n)].strip() for n in used}
        return self
