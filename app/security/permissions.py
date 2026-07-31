"""Simple permission helpers (not a full RBAC framework)."""
from __future__ import annotations

from fastapi import HTTPException

ROLE_PERMS: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {
            "manage_users",
            "manage_campaigns",
            "manage_templates",
            "export_leads",
            "view_audit",
            "change_account_settings",
            "manage_agents",
            "campaigns.create",
            "campaigns.manage",
            "campaigns.start",
            "campaigns.approve_ai",
            "campaigns.use_no_review",
            "agents.use_in_campaigns",
            "ai.view_costs",
        }
    ),
    "moderator": frozenset(
        {
            "manage_campaigns",
            "manage_templates",
            "export_leads",
            "view_audit",
            "change_account_settings",
            "manage_agents",
            "campaigns.create",
            "campaigns.manage",
            "campaigns.start",
            "campaigns.approve_ai",
            "campaigns.use_no_review",
            "agents.use_in_campaigns",
            "ai.view_costs",
        }
    ),
    "agent": frozenset(
        {
            "manage_campaigns",
            "manage_templates",
            "export_leads",
            "change_account_settings",
            "campaigns.create",
            "campaigns.manage",
            "campaigns.start",
            "campaigns.approve_ai",
            "agents.use_in_campaigns",
        }
    ),
    "user": frozenset(
        {
            "manage_campaigns",
            "manage_templates",
            "export_leads",
            "change_account_settings",
            "campaigns.create",
            "campaigns.manage",
            "campaigns.start",
            "campaigns.approve_ai",
            "agents.use_in_campaigns",
        }
    ),
}


def normalize_role(role: str | None) -> str:
    r = (role or "user").strip().lower()
    if r == "agent":
        return "agent"
    if r in ROLE_PERMS:
        return r
    return "user"


def has_permission(user: dict, perm: str) -> bool:
    role = normalize_role(user.get("role"))
    return perm in ROLE_PERMS.get(role, ROLE_PERMS["user"])


def require_permission(user: dict, perm: str) -> None:
    if not has_permission(user, perm):
        raise HTTPException(status_code=403, detail="Permission denied")
