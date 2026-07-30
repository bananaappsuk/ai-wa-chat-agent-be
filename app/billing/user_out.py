"""Shared UserOut mapping helpers."""

from __future__ import annotations

from app.billing.plans import normalize_plan_key
from app.models.common import serialize
from app.models.user import UserOut


def _iso_maybe(val) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val) if val else None


def user_out_from_doc(doc: dict) -> UserOut:
    s = serialize(doc)
    role = s.get("role") or "user"
    if role not in ("user", "agent", "moderator", "admin"):
        role = "user"
    trial_end = s.get("trial_ends_at") or s.get("trial_end")
    return UserOut(
        id=s["id"],
        email=s["email"],
        full_name=s.get("full_name") or "",
        first_name=s.get("first_name"),
        last_name=s.get("last_name"),
        display_name=s.get("display_name"),
        company_name=s.get("company_name"),
        phone=s.get("phone"),
        twilio_whatsapp_to=s.get("twilio_whatsapp_to"),
        timezone=s.get("timezone"),
        locale=s.get("locale"),
        avatar_url=s.get("avatar_url"),
        notification_preferences=s.get("notification_preferences"),
        plan=normalize_plan_key(s.get("plan", "free")),
        subscription_status=(s.get("subscription_status") or "none"),
        stripe_customer_id=s.get("stripe_customer_id"),
        stripe_subscription_id=s.get("stripe_subscription_id"),
        stripe_price_id=s.get("stripe_price_id"),
        stripe_product_id=s.get("stripe_product_id"),
        trial_start=_iso_maybe(s.get("trial_start")),
        trial_ends_at=_iso_maybe(trial_end),
        current_period_start=_iso_maybe(s.get("current_period_start")),
        current_period_end=_iso_maybe(s.get("current_period_end")),
        cancel_at_period_end=bool(s.get("cancel_at_period_end")),
        cancelled_at=_iso_maybe(s.get("cancelled_at")),
        subscription_created_at=_iso_maybe(s.get("subscription_created_at")),
        subscription_updated_at=_iso_maybe(s.get("subscription_updated_at")),
        latest_invoice_id=s.get("latest_invoice_id"),
        last_payment_status=s.get("last_payment_status"),
        last_payment_at=_iso_maybe(s.get("last_payment_at")),
        role=role,
        banned=bool(s.get("banned", False)),
        active=s.get("active") is not False,
        last_login_at=s.get("last_login_at"),
        created_at=s.get("created_at"),
        updated_at=s.get("updated_at"),
    )
