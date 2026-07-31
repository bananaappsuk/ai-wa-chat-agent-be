from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


CampaignStatus = Literal[
    "draft",
    "scheduled",
    "queued",
    "running",
    "paused",
    "completed",
    "partially_completed",
    "failed",
    "cancelled",
]

EDITABLE_STATUSES = frozenset({"draft", "scheduled"})
TERMINAL_STATUSES = frozenset({"completed", "partially_completed", "failed", "cancelled"})

ContentMode = Literal["template", "ai_agent"]
ReviewMode = Literal["sample_review", "full_review", "no_manual_review"]
FallbackAction = Literal["use_static_template", "require_manual_review", "skip"]
AiContextMode = Literal[
    "campaign_only",
    "campaign_and_lead_profile",
    "campaign_and_summary",
    "campaign_and_recent_chat",
    "custom",
]
DeliveryScope = Literal["open_window_only", "all_eligible_recipients", "template_only"]
KnowledgeScope = Literal["none", "selected", "topic_matched"]
AiGenerationStatus = Literal[
    "idle",
    "pending",
    "generating",
    "ready",
    "partial",
    "failed",
    "needs_review",
]
RecipientContentSource = Literal["template", "ai_freeform", "ai_template_variables", "knowledge_base"]

_VAGUE_GOALS = frozenset(
    {
        "personalised whatsapp outreach to opted-in leads",
        "personalized whatsapp outreach to opted-in leads",
        "personalised outreach",
        "personalized outreach",
    }
)


def _topic_list(values: Optional[list[str]], *, max_items: int = 20, max_len: int = 80) -> list[str]:
    out: list[str] = []
    for raw in values or []:
        t = (raw or "").strip()
        if not t:
            continue
        out.append(t[:max_len])
        if len(out) >= max_items:
            break
    return out


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    message: Optional[str] = Field(default=None, max_length=1600)
    media_url: Optional[str] = Field(default=None, max_length=500)
    media_content_type: Optional[str] = Field(default=None, max_length=100)
    template_id: Optional[str] = None
    content_variables: Optional[dict[str, str]] = None
    scheduled_at: Optional[str] = None
    lead_ids: Optional[list[str]] = None
    recipients: Optional[list[str]] = None
    recipient_source: Optional[str] = Field(default="manual", max_length=50)
    status: Optional[str] = "draft"

    content_mode: ContentMode = "template"
    agent_id: Optional[str] = Field(default=None, max_length=40)
    campaign_subject: Optional[str] = Field(default=None, max_length=200)
    campaign_goal: Optional[str] = Field(default=None, max_length=500)
    campaign_instructions: Optional[str] = Field(default=None, max_length=4000)
    campaign_language: Optional[str] = Field(default=None, max_length=20)
    campaign_tone_override: Optional[str] = Field(default=None, max_length=40)
    review_mode: ReviewMode = "sample_review"
    preview_count: int = Field(default=5, ge=1, le=25)
    fallback_template_id: Optional[str] = None
    allow_freeform_inside_window: bool = True
    personalise_template_variables: bool = True
    max_ai_output_tokens: Optional[int] = Field(default=None, ge=50, le=2000)
    ai_temperature_override: Optional[float] = Field(default=None, ge=0.0, le=1.5)
    require_approval_before_start: bool = True
    on_ai_failure: FallbackAction = "use_static_template"
    on_moderation_block: FallbackAction = "require_manual_review"
    on_low_confidence: FallbackAction = "require_manual_review"
    on_quota_exceeded: FallbackAction = "require_manual_review"
    on_window_closed_before_send: FallbackAction = "use_static_template"

    # Context / knowledge / delivery isolation
    ai_context_mode: AiContextMode = "campaign_only"
    include_lead_profile: bool = False
    include_conversation_summary: bool = False
    include_recent_messages: bool = False
    recent_message_limit: int = Field(default=4, ge=1, le=10)
    knowledge_scope: KnowledgeScope = "none"
    campaign_knowledge_text: Optional[str] = Field(default=None, max_length=8000)
    campaign_knowledge_source_ids: Optional[list[str]] = None
    required_topics: Optional[list[str]] = None
    prohibited_topics: Optional[list[str]] = None
    delivery_scope: DeliveryScope = "all_eligible_recipients"

    @field_validator("required_topics", "prohibited_topics", mode="before")
    @classmethod
    def _norm_topics(cls, v: Any) -> Optional[list[str]]:
        if v is None:
            return None
        if isinstance(v, str):
            parts = [p.strip() for p in v.replace("\n", ",").split(",")]
            return _topic_list(parts)
        if isinstance(v, list):
            return _topic_list([str(x) for x in v])
        return None

    @model_validator(mode="after")
    def require_content(self) -> "CampaignCreate":
        mode = (self.content_mode or "template").strip().lower()
        if mode == "ai_agent":
            if not (self.agent_id or "").strip():
                raise ValueError("agent_id is required for AI Agent campaigns")
            goal = (self.campaign_goal or "").strip()
            # Subject defaults from campaign name or goal — keep create form simple
            subject = (self.campaign_subject or "").strip() or (self.name or "").strip() or goal[:80]
            if not goal:
                raise ValueError("campaign_goal is required for AI Agent campaigns")
            if len(goal) < 8:
                raise ValueError("campaign_goal is too short — describe what the agent should promote")
            if goal.lower() in _VAGUE_GOALS:
                raise ValueError(
                    "campaign_goal is too vague — name the specific subject "
                    "(e.g. invite to AI Summer Camp Essentials 2026 only)"
                )
            self.campaign_subject = subject[:200]
            scope = (self.delivery_scope or "all_eligible_recipients").strip().lower()
            self.delivery_scope = scope  # type: ignore[assignment]
            has_fallback = bool((self.fallback_template_id or self.template_id or "").strip())
            if scope in ("all_eligible_recipients", "template_only") and not has_fallback:
                raise ValueError(
                    "An approved fallback/template is required for this delivery scope"
                )
            if scope == "open_window_only":
                self.on_window_closed_before_send = "skip"
                self.allow_freeform_inside_window = True
            elif scope == "template_only":
                self.on_window_closed_before_send = "use_static_template"
                self.allow_freeform_inside_window = False
            else:
                self.on_window_closed_before_send = "use_static_template"
                self.allow_freeform_inside_window = True
            # Sync boolean context flags from mode for custom clarity
            if self.ai_context_mode == "campaign_only":
                self.include_lead_profile = False
                self.include_conversation_summary = False
                self.include_recent_messages = False
            elif self.ai_context_mode == "campaign_and_lead_profile":
                self.include_lead_profile = True
                self.include_conversation_summary = False
                self.include_recent_messages = False
            elif self.ai_context_mode == "campaign_and_summary":
                self.include_lead_profile = True
                self.include_conversation_summary = True
                self.include_recent_messages = False
            elif self.ai_context_mode == "campaign_and_recent_chat":
                self.include_lead_profile = True
                self.include_conversation_summary = False
                self.include_recent_messages = True
            return self
        has_template = bool((self.template_id or "").strip())
        has_body = bool((self.message or "").strip())
        has_media = bool((self.media_url or "").strip())
        if has_template and has_media:
            raise ValueError("Cannot attach media to template campaigns")
        if not has_template and not has_body and not has_media:
            raise ValueError("Provide message text, media, or an approved template")
        return self


class CampaignUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    message: Optional[str] = Field(default=None, max_length=1600)
    media_url: Optional[str] = Field(default=None, max_length=500)
    media_content_type: Optional[str] = Field(default=None, max_length=100)
    template_id: Optional[str] = None
    content_variables: Optional[dict[str, str]] = None
    scheduled_at: Optional[str] = None
    lead_ids: Optional[list[str]] = None
    recipients: Optional[list[str]] = None
    recipient_source: Optional[str] = None
    status: Optional[str] = None

    content_mode: Optional[ContentMode] = None
    agent_id: Optional[str] = Field(default=None, max_length=40)
    campaign_subject: Optional[str] = Field(default=None, max_length=200)
    campaign_goal: Optional[str] = Field(default=None, max_length=500)
    campaign_instructions: Optional[str] = Field(default=None, max_length=4000)
    campaign_language: Optional[str] = Field(default=None, max_length=20)
    campaign_tone_override: Optional[str] = Field(default=None, max_length=40)
    review_mode: Optional[ReviewMode] = None
    preview_count: Optional[int] = Field(default=None, ge=1, le=25)
    fallback_template_id: Optional[str] = None
    allow_freeform_inside_window: Optional[bool] = None
    personalise_template_variables: Optional[bool] = None
    max_ai_output_tokens: Optional[int] = Field(default=None, ge=50, le=2000)
    ai_temperature_override: Optional[float] = Field(default=None, ge=0.0, le=1.5)
    require_approval_before_start: Optional[bool] = None
    on_ai_failure: Optional[FallbackAction] = None
    on_moderation_block: Optional[FallbackAction] = None
    on_low_confidence: Optional[FallbackAction] = None
    on_quota_exceeded: Optional[FallbackAction] = None
    on_window_closed_before_send: Optional[FallbackAction] = None

    ai_context_mode: Optional[AiContextMode] = None
    include_lead_profile: Optional[bool] = None
    include_conversation_summary: Optional[bool] = None
    include_recent_messages: Optional[bool] = None
    recent_message_limit: Optional[int] = Field(default=None, ge=1, le=10)
    knowledge_scope: Optional[KnowledgeScope] = None
    campaign_knowledge_text: Optional[str] = Field(default=None, max_length=8000)
    campaign_knowledge_source_ids: Optional[list[str]] = None
    required_topics: Optional[list[str]] = None
    prohibited_topics: Optional[list[str]] = None
    delivery_scope: Optional[DeliveryScope] = None

    @field_validator("required_topics", "prohibited_topics", mode="before")
    @classmethod
    def _norm_topics(cls, v: Any) -> Optional[list[str]]:
        if v is None:
            return None
        if isinstance(v, str):
            parts = [p.strip() for p in v.replace("\n", ",").split(",")]
            return _topic_list(parts)
        if isinstance(v, list):
            return _topic_list([str(x) for x in v])
        return None


class AiPreviewRequest(BaseModel):
    preview_count: Optional[int] = Field(default=None, ge=1, le=25)
    regenerate: bool = False
    selected_lead_ids: Optional[list[str]] = None


class BlastCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    message: Optional[str] = Field(default=None, max_length=1600)
    recipients: list[str] = Field(min_length=1, max_length=5000)
    template_id: Optional[str] = None
    content_variables: Optional[dict[str, str]] = None
    media_url: Optional[str] = Field(default=None, max_length=500)
    message_purpose: Optional[str] = Field(default=None, max_length=40)


def empty_campaign_counters() -> dict[str, Any]:
    return {
        "total_recipients": 0,
        "queued_count": 0,
        "processing_count": 0,
        "sent_count": 0,
        "delivered_count": 0,
        "read_count": 0,
        "failed_count": 0,
        "cancelled_count": 0,
        "skipped_count": 0,
        "replied_count": 0,
        "progress_percentage": 0.0,
        "started_at": None,
        "completed_at": None,
        "paused_at": None,
        "cancelled_at": None,
        "last_error": None,
        "ai_ready_count": 0,
        "ai_review_count": 0,
        "ai_failed_count": 0,
        "ai_freeform_count": 0,
        "ai_template_var_count": 0,
        "ai_static_fallback_count": 0,
        "ai_input_tokens_total": 0,
        "ai_output_tokens_total": 0,
        "ai_estimated_cost_total": 0.0,
    }


def campaign_ai_defaults() -> dict[str, Any]:
    """Defaults applied on create for AI-related campaign fields."""
    return {
        "content_mode": "template",
        "agent_id": None,
        "campaign_subject": None,
        "campaign_goal": None,
        "campaign_instructions": None,
        "campaign_language": None,
        "campaign_tone_override": None,
        "review_mode": "sample_review",
        "preview_count": 5,
        "ai_generation_status": "idle",
        "ai_generation_started_at": None,
        "ai_generation_completed_at": None,
        "fallback_template_id": None,
        "fallback_template_content_sid": None,
        "allow_freeform_inside_window": True,
        "personalise_template_variables": True,
        "max_ai_output_tokens": None,
        "ai_temperature_override": None,
        "require_approval_before_start": True,
        "approved_at": None,
        "approved_by": None,
        "agent_snapshot": None,
        "on_ai_failure": "use_static_template",
        "on_moderation_block": "require_manual_review",
        "on_low_confidence": "require_manual_review",
        "on_quota_exceeded": "require_manual_review",
        "on_window_closed_before_send": "use_static_template",
        # Isolation defaults — never silently enable Live Chat context
        "ai_context_mode": "campaign_only",
        "include_lead_profile": False,
        "include_conversation_summary": False,
        "include_recent_messages": False,
        "recent_message_limit": 4,
        "knowledge_scope": "none",
        "campaign_knowledge_text": None,
        "campaign_knowledge_source_ids": [],
        "knowledge_snapshot": None,
        "required_topics": [],
        "prohibited_topics": [],
        "delivery_scope": "all_eligible_recipients",
    }
