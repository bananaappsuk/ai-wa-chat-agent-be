from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator

# Live Chat / template-consent: marketing templates must never silently
# fall back to "conversational" — the sender must explicitly pick a purpose.
TEMPLATE_ALLOWED_PURPOSES = frozenset({"conversational", "support", "transactional", "marketing"})


class MessageSend(BaseModel):
    lead_id: str
    message: Optional[str] = Field(default=None, max_length=1600)
    media_url: Optional[str] = None
    media_content_type: Optional[str] = None
    media_filename: Optional[str] = None
    template_id: Optional[str] = None
    content_sid: Optional[str] = None
    content_variables: Optional[dict[str, str]] = None
    # No default: templates MUST explicitly set this (enforced in the route,
    # which returns a clean 400). Non-template sends default to conversational.
    message_purpose: Optional[str] = Field(default=None, max_length=40)

    @model_validator(mode="after")
    def require_body_template_or_media(self) -> "MessageSend":
        has_template = bool(self.template_id or self.content_sid)
        has_body = bool((self.message or "").strip())
        has_media = bool((self.media_url or "").strip())
        if has_template and has_media:
            raise ValueError("Cannot attach media to template messages")
        if not has_template and not has_body and not has_media:
            raise ValueError("Provide message text, media, or an approved template")
        if self.message_purpose is not None:
            self.message_purpose = self.message_purpose.strip().lower() or None
        if not has_template and not self.message_purpose:
            self.message_purpose = "conversational"
        return self


class MessageDoc(BaseModel):
    lead_id: str
    user_id: str
    direction: Literal["inbound", "outbound"]
    message: str
    status: str = "sent"
    message_type: Optional[str] = "text"
    media_url: Optional[str] = None
    media_content_type: Optional[str] = None
    media_filename: Optional[str] = None
    twilio_sid: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    provider_status: Optional[str] = None
    status_updated_at: Optional[str] = None
    sent_at: Optional[str] = None
    delivered_at: Optional[str] = None
    read_at: Optional[str] = None
    failed_at: Optional[str] = None
