"""Plan entitlement resolution + enforcement.

Entitlements are defined per plan in ``app/billing/plans.py``. This module turns
them into real gates.

ENFORCED (features that actually exist in the product):
- ai_agents              (count) -> agent creation
- campaigns              (bool)  -> campaign create / start
- whatsapp_broadcast     (bool)  -> blast create
- ai_conversations_month (count) -> distinct leads that receive an AI auto-reply
                                     per calendar month (a whole back-and-forth thread
                                     counts as ONE conversation)
- whatsapp_numbers       (count) -> connecting a WhatsApp sender. INTERPRETATION:
      enforced as free-vs-paid only. A limit of 0 (free) cannot connect any sender;
      a paid limit (>=1 or -1) may connect. We deliberately do NOT block a paid tenant
      from wiring BOTH Twilio and Meta for their one number, since the two are just
      alternate routes to the same WhatsApp presence. Tighten here if per-provider
      caps are ever required.

NOT ENFORCED (no corresponding product feature yet): team_members (no teams/seats),
unlimited_contacts (no numeric cap defined), api_access, webhooks, custom_branding,
sso, white_label.

A limit value of -1 means unlimited.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from app.billing.plans import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    PLAN_CATALOG,
    entitlements_for_plan,
    normalize_plan_key,
)

# Statuses that grant the paid plan's entitlements. Admin manual overrides set
# subscription_status="manual_override"; Stripe grace period keeps past_due entitled.
ENTITLED_STATUSES = frozenset(ACTIVE_SUBSCRIPTION_STATUSES | {"manual_override"})

_AICONV_TTL_SECONDS = 40 * 24 * 3600  # a bit over a month so the set survives the billing period


def effective_plan_key(user: Optional[dict]) -> str:
    """The plan whose entitlements currently apply. A paid plan only counts while the
    subscription is in an entitled state; otherwise the user falls back to free."""
    plan = normalize_plan_key((user or {}).get("plan"))
    if plan == "free":
        return "free"
    status = ((user or {}).get("subscription_status") or "none").strip().lower()
    return plan if status in ENTITLED_STATUSES else "free"


def effective_entitlements(user: Optional[dict]) -> dict[str, Any]:
    return entitlements_for_plan(effective_plan_key(user))


def plan_display_name(plan_key: str) -> str:
    return PLAN_CATALOG.get(plan_key, PLAN_CATALOG["free"])["name"]


class EntitlementError(HTTPException):
    """402 Payment Required with a structured, FE-friendly body."""

    def __init__(
        self,
        *,
        entitlement: str,
        plan: str,
        message: str,
        limit: Any = None,
        current: Any = None,
    ) -> None:
        super().__init__(
            status_code=402,
            detail={
                "code": "entitlement_required",
                "entitlement": entitlement,
                "plan": plan,
                "limit": limit,
                "current": current,
                "message": message,
            },
        )


def require_feature(user: dict, key: str, *, label: str) -> None:
    """Gate a boolean entitlement (campaigns, whatsapp_broadcast)."""
    plan = effective_plan_key(user)
    if not effective_entitlements(user).get(key):
        raise EntitlementError(
            entitlement=key,
            plan=plan,
            message=f"{label} isn't included in your {plan_display_name(plan)} plan. Upgrade to unlock it.",
        )


def require_capacity(user: dict, key: str, current_count: int, *, label: str) -> None:
    """Gate a counted entitlement (ai_agents). -1 == unlimited."""
    plan = effective_plan_key(user)
    limit = effective_entitlements(user).get(key, 0)
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = 0
    if limit_i < 0:
        return
    if current_count >= limit_i:
        if limit_i == 0:
            msg = f"{label} aren't included in your {plan_display_name(plan)} plan. Upgrade to add them."
        else:
            msg = f"You've reached your {plan_display_name(plan)} plan limit of {limit_i} {label}. Upgrade for more."
        raise EntitlementError(
            entitlement=key, plan=plan, message=msg, limit=limit_i, current=current_count
        )


def require_can_connect_number(user: dict) -> None:
    """Gate connecting a WhatsApp sender (Twilio number or Meta onboarding). Free-vs-paid."""
    plan = effective_plan_key(user)
    limit = effective_entitlements(user).get("whatsapp_numbers", 0)
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = 0
    if limit_i == 0:
        raise EntitlementError(
            entitlement="whatsapp_numbers",
            plan=plan,
            message=(
                "Connecting a WhatsApp number requires a paid plan. "
                "Upgrade to connect Twilio or Meta WhatsApp."
            ),
            limit=0,
        )


def connected_number_count(user: dict) -> int:
    """Distinct connected WhatsApp senders (Twilio routing number + Meta connection)."""
    n = 0
    if (user.get("twilio_whatsapp_to") or "").strip():
        n += 1
    meta_status = (user.get("meta_connection_status") or "").strip().lower()
    if meta_status in ("connected", "legacy_poc") or (user.get("meta_phone_number_id") or "").strip():
        n += 1
    return n


# --- Monthly AI-conversation meter (distinct leads per tenant per calendar month) ----

def _aiconv_key(tenant_id: str, now: Optional[datetime] = None) -> str:
    ts = now or datetime.now(timezone.utc)
    return f"entitlement:aiconv:{tenant_id}:{ts.strftime('%Y%m')}"


def ai_conversation_allowed(
    redis, tenant_id: str, lead_id: str, limit: Any, *, now: Optional[datetime] = None
) -> bool:
    """True if this lead may receive an AI reply this month. A lead already counted this
    month is always allowed (a whole thread is one conversation). Fails OPEN on Redis
    errors so a Redis blip never silences the agent."""
    try:
        limit_i = int(limit)
    except (TypeError, ValueError):
        limit_i = 0
    if limit_i < 0:
        return True
    if limit_i == 0:
        return False
    try:
        key = _aiconv_key(tenant_id, now)
        if redis.sismember(key, str(lead_id)):
            return True
        return int(redis.scard(key) or 0) < limit_i
    except Exception:
        return True


def record_ai_conversation(redis, tenant_id: str, lead_id: str, *, now: Optional[datetime] = None) -> None:
    """Idempotently count this lead as an AI conversation for the month (SADD dedupes)."""
    try:
        key = _aiconv_key(tenant_id, now)
        pipe = redis.pipeline()
        pipe.sadd(key, str(lead_id))
        pipe.expire(key, _AICONV_TTL_SECONDS)
        pipe.execute()
    except Exception:
        pass


def ai_conversation_count(redis, tenant_id: str, *, now: Optional[datetime] = None) -> int:
    try:
        return int(redis.scard(_aiconv_key(tenant_id, now)) or 0)
    except Exception:
        return 0


async def usage_snapshot(db, redis, user: dict) -> dict[str, Any]:
    """Current usage vs effective limits, for the billing UI."""
    uid = str(user["_id"])
    plan = effective_plan_key(user)
    ent = effective_entitlements(user)
    agents = await db.agents.count_documents({"user_id": uid})
    return {
        "plan": plan,
        "plan_name": plan_display_name(plan),
        "items": {
            "ai_agents": {"used": agents, "limit": ent.get("ai_agents", 0)},
            "whatsapp_numbers": {
                "used": connected_number_count(user),
                "limit": ent.get("whatsapp_numbers", 0),
            },
            "ai_conversations_month": {
                "used": ai_conversation_count(redis, uid),
                "limit": ent.get("ai_conversations_month", 0),
            },
            "campaigns": {"enabled": bool(ent.get("campaigns"))},
            "whatsapp_broadcast": {"enabled": bool(ent.get("whatsapp_broadcast"))},
        },
    }
