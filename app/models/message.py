from typing import Literal, Optional
from pydantic import BaseModel, Field


class MessageSend(BaseModel):
    lead_id: str
    message: str = Field(min_length=1, max_length=1600)
    media_url: Optional[str] = None


class MessageDoc(BaseModel):
    lead_id: str
    user_id: str
    direction: Literal["inbound", "outbound"]
    message: str
    status: str = "sent"
    twilio_sid: Optional[str] = None
    error: Optional[str] = None
