from pydantic import BaseModel, EmailStr, Field, field_validator
from typing import Any, Literal, Optional


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=100)
    company_name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)
    twilio_whatsapp_to: Optional[str] = Field(default=None, max_length=32)

    @field_validator("password")
    @classmethod
    def password_complexity(cls, v: str) -> str:
        import re

        if not re.search(r"[A-Za-z]", v) or not re.search(r"\d", v):
            raise ValueError("Password must include at least one letter and one number")
        return v


class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: str
    email: EmailStr
    full_name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    display_name: Optional[str] = None
    company_name: Optional[str] = None
    phone: Optional[str] = None
    twilio_whatsapp_to: Optional[str] = None
    timezone: Optional[str] = None
    locale: Optional[str] = None
    avatar_url: Optional[str] = None
    notification_preferences: Optional[dict[str, Any]] = None
    plan: str = "free"
    subscription_status: Optional[str] = "none"
    stripe_customer_id: Optional[str] = None
    stripe_subscription_id: Optional[str] = None
    stripe_price_id: Optional[str] = None
    stripe_product_id: Optional[str] = None
    trial_start: Optional[str] = None
    trial_ends_at: Optional[str] = None
    current_period_start: Optional[str] = None
    current_period_end: Optional[str] = None
    cancel_at_period_end: bool = False
    cancelled_at: Optional[str] = None
    subscription_created_at: Optional[str] = None
    subscription_updated_at: Optional[str] = None
    latest_invoice_id: Optional[str] = None
    last_payment_status: Optional[str] = None
    last_payment_at: Optional[str] = None
    role: Literal["user", "agent", "moderator", "admin"] = "user"
    banned: bool = False
    active: bool = True
    last_login_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class ProfileUpdate(BaseModel):
    full_name: Optional[str] = Field(default=None, max_length=100)
    first_name: Optional[str] = Field(default=None, max_length=50)
    last_name: Optional[str] = Field(default=None, max_length=50)
    display_name: Optional[str] = Field(default=None, max_length=100)
    company_name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)
    twilio_whatsapp_to: Optional[str] = Field(default=None, max_length=32)
    timezone: Optional[str] = Field(default=None, max_length=64)
    locale: Optional[str] = Field(default=None, max_length=16)
    notification_preferences: Optional[dict[str, bool]] = None
    email: Optional[EmailStr] = None
    current_password: Optional[str] = Field(default=None, max_length=128)


class ForgotPasswordBody(BaseModel):
    email: EmailStr


class ResetPasswordBody(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str = Field(min_length=8, max_length=128)


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)
