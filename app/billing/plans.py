"""Single source of truth for SaaS plans. Entitlements are returned to clients but not enforced yet."""

from __future__ import annotations

from typing import Any, Optional

# Legacy admin/UI key → canonical catalog key
_PLAN_ALIASES = {
    "pro": "professional",
    "prof": "professional",
}

PAID_PLAN_KEYS = frozenset({"starter", "professional", "business"})
ALL_PLAN_KEYS = frozenset({"free", "starter", "professional", "business", "enterprise"})
ACTIVE_SUBSCRIPTION_STATUSES = frozenset({"trialing", "active", "past_due"})

PLAN_CATALOG: dict[str, dict[str, Any]] = {
    "free": {
        "key": "free",
        "name": "Free",
        "price_gbp": 0,
        "price_display": "£0",
        "period": "/mo",
        "popular": False,
        "stripe_checkout": False,
        "contact_sales": False,
        "features": [
            "Limited access",
            "Explore the product",
            "Upgrade anytime",
        ],
        "entitlements": {
            "whatsapp_numbers": 0,
            "team_members": 1,
            "ai_agents": 0,
            "ai_conversations_month": 0,
            "unlimited_contacts": False,
            "campaigns": False,
            "whatsapp_broadcast": False,
            "api_access": False,
            "webhooks": False,
            "custom_branding": False,
            "sso": False,
            "white_label": False,
        },
    },
    "starter": {
        "key": "starter",
        "name": "Starter",
        "price_gbp": 39,
        "price_display": "£39",
        "period": "/mo",
        "popular": False,
        "stripe_checkout": True,
        "contact_sales": False,
        "features": [
            "1 WhatsApp Number",
            "2 Team Members",
            "Live Chat",
            "AI Auto Replies",
            "Lead & Contact Management",
            "Conversation History",
            "Marketing Consent",
            "Basic Analytics",
            "500 AI Conversations/month",
            "Email Support",
        ],
        "entitlements": {
            "whatsapp_numbers": 1,
            "team_members": 2,
            "ai_agents": 1,
            "ai_conversations_month": 500,
            "unlimited_contacts": False,
            "campaigns": False,
            "whatsapp_broadcast": False,
            "api_access": False,
            "webhooks": False,
            "custom_branding": False,
            "sso": False,
            "white_label": False,
        },
    },
    "professional": {
        "key": "professional",
        "name": "Professional",
        "price_gbp": 79,
        "price_display": "£79",
        "period": "/mo",
        "popular": True,
        "stripe_checkout": True,
        "contact_sales": False,
        "features": [
            "Everything in Starter",
            "5 Team Members",
            "Unlimited Contacts",
            "AI Knowledge Base",
            "Multiple AI Agents",
            "Campaign Management",
            "WhatsApp Broadcast",
            "Scheduled Campaigns",
            "Campaign Analytics",
            "Customer Segments & Tags",
            "Blacklist Management",
            "File Uploads",
            "5,000 AI Conversations/month",
            "Priority Support",
        ],
        "entitlements": {
            "whatsapp_numbers": 1,
            "team_members": 5,
            "ai_agents": 5,
            "ai_conversations_month": 5000,
            "unlimited_contacts": True,
            "campaigns": True,
            "whatsapp_broadcast": True,
            "api_access": False,
            "webhooks": False,
            "custom_branding": False,
            "sso": False,
            "white_label": False,
        },
    },
    "business": {
        "key": "business",
        "name": "Business",
        "price_gbp": 149,
        "price_display": "£149",
        "period": "/mo",
        "popular": False,
        "stripe_checkout": True,
        "contact_sales": False,
        "features": [
            "Everything in Professional",
            "15 Team Members",
            "Unlimited AI Agents",
            "Unlimited Knowledge Bases",
            "Team Inbox",
            "Roles & Permissions",
            "API Access",
            "Webhooks",
            "CRM Integrations",
            "Custom Branding",
            "AI Campaign Personalisation",
            "25,000 AI Conversations/month",
            "Priority Support",
        ],
        "entitlements": {
            "whatsapp_numbers": 1,
            "team_members": 15,
            "ai_agents": -1,
            "ai_conversations_month": 25000,
            "unlimited_contacts": True,
            "campaigns": True,
            "whatsapp_broadcast": True,
            "api_access": True,
            "webhooks": True,
            "custom_branding": True,
            "sso": False,
            "white_label": False,
        },
    },
    "enterprise": {
        "key": "enterprise",
        "name": "Enterprise",
        "price_gbp": None,
        "price_display": "Custom",
        "period": "",
        "popular": False,
        "stripe_checkout": False,
        "contact_sales": True,
        "features": [
            "Unlimited Users",
            "Unlimited WhatsApp Numbers",
            "Unlimited AI Conversations",
            "White Label",
            "Dedicated Infrastructure",
            "Single Sign-On (SSO)",
            "Audit Logs",
            "Advanced Security",
            "Custom Integrations",
            "Dedicated Account Manager",
            "SLA Support",
        ],
        "entitlements": {
            "whatsapp_numbers": -1,
            "team_members": -1,
            "ai_agents": -1,
            "ai_conversations_month": -1,
            "unlimited_contacts": True,
            "campaigns": True,
            "whatsapp_broadcast": True,
            "api_access": True,
            "webhooks": True,
            "custom_branding": True,
            "sso": True,
            "white_label": True,
        },
    },
}


def normalize_plan_key(raw: Optional[str]) -> str:
    key = (raw or "free").strip().lower()
    key = _PLAN_ALIASES.get(key, key)
    if key not in ALL_PLAN_KEYS:
        return "free"
    return key


def price_id_for_plan(plan_key: str) -> Optional[str]:
    from app.billing.stripe_config import get_stripe_runtime

    key = normalize_plan_key(plan_key)
    return get_stripe_runtime().price_for_plan(key)


def plan_from_price_id(price_id: Optional[str]) -> Optional[str]:
    from app.billing.stripe_config import get_stripe_runtime

    return get_stripe_runtime().plan_from_price(price_id)


def public_plans(*, include_free: bool = False) -> list[dict[str, Any]]:
    keys = ["starter", "professional", "business", "enterprise"]
    if include_free:
        keys = ["free", *keys]
    out: list[dict[str, Any]] = []
    for key in keys:
        plan = PLAN_CATALOG[key]
        out.append(
            {
                "key": plan["key"],
                "name": plan["name"],
                "price_gbp": plan["price_gbp"],
                "price_display": plan["price_display"],
                "period": plan["period"],
                "popular": plan["popular"],
                "stripe_checkout": plan["stripe_checkout"],
                "contact_sales": plan["contact_sales"],
                "features": list(plan["features"]),
                "entitlements": dict(plan["entitlements"]),
                "cta": "Contact Sales" if plan["contact_sales"] else "Start free trial",
            }
        )
    return out


def entitlements_for_plan(plan_key: str) -> dict[str, Any]:
    key = normalize_plan_key(plan_key)
    return dict(PLAN_CATALOG.get(key, PLAN_CATALOG["free"])["entitlements"])
