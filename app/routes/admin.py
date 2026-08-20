from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Literal, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from pymongo.errors import DuplicateKeyError

from app.billing.plans import ALL_PLAN_KEYS, normalize_plan_key
from app.db.mongo import get_db
from app.middleware.auth import current_admin, hash_password
from app.middleware.security import get_request_id
from app.models.common import serialize, utcnow
from app.security.audit import audit
from app.security.permissions import require_permission
from app.security.validation import require_object_id, validate_password_complexity
from app.services.activity import record_activity
from app.services.meta_credentials import delete_credentials_for_user
from app.services.password_reset import issue_reset_token

router = APIRouter(prefix="/admin", tags=["admin"])

_ALLOWED_PLANS = frozenset(ALL_PLAN_KEYS) | frozenset({"pro"})  # legacy alias
_ALLOWED_ROLES = frozenset({"user", "agent", "moderator", "admin"})
_ADMIN_PATCH_ALLOW = frozenset(
    {
        "full_name",
        "email",
        "role",
        "plan",
        "active",
        "company_name",
        "phone",
    }
)


class RoleBody(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in _ALLOWED_ROLES:
            raise ValueError("Invalid role")
        return v


class PlanBody(BaseModel):
    plan: str = Field(max_length=40)
    manual_override: bool = False

    @field_validator("plan")
    @classmethod
    def validate_plan(cls, v: str) -> str:
        p = (v or "").strip().lower()
        if p not in _ALLOWED_PLANS:
            raise ValueError("Invalid plan")
        return normalize_plan_key(p)


class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=100)
    role: Literal["user", "agent", "moderator", "admin"] = "user"
    plan: str = "free"
    company_name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)
    active: bool = True


class AdminUserPatch(BaseModel):
    full_name: Optional[str] = Field(default=None, max_length=100)
    email: Optional[EmailStr] = None
    role: Optional[str] = None
    plan: Optional[str] = None
    active: Optional[bool] = None
    company_name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if v not in _ALLOWED_ROLES:
            raise ValueError("Invalid role")
        return v

    @field_validator("plan")
    @classmethod
    def validate_plan(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        p = v.strip().lower()
        if p not in _ALLOWED_PLANS:
            raise ValueError("Invalid plan")
        return normalize_plan_key(p)


def _safe_user(doc: dict) -> dict:
    s = serialize(doc)
    s.pop("password_hash", None)
    s.pop("password_changed_at", None)
    s["roles"] = [s.get("role", "user")]
    s["active"] = s.get("active") is not False and not s.get("banned")
    return s


async def _active_admin_count(db) -> int:
    return int(
        await db.users.count_documents(
            {
                "role": "admin",
                "banned": {"$ne": True},
                "active": {"$ne": False},
            }
        )
    )


def _parse_dt(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid date") from exc


@router.get("/users")
async def list_users(
    admin: dict = Depends(current_admin),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
    role: Optional[str] = Query(default=None, max_length=20),
    active: Optional[str] = Query(default=None, max_length=10),
    created_from: Optional[str] = Query(default=None),
    created_to: Optional[str] = Query(default=None),
    sort: str = Query(default="-created_at", max_length=40),
    # Legacy pagination
    limit: Optional[int] = Query(default=None, ge=1, le=500),
    offset: Optional[int] = Query(default=None, ge=0, le=100_000),
):
    require_permission(admin, "manage_users")

    sort_field = "created_at"
    sort_dir = -1
    raw_sort = (sort or "-created_at").strip()
    if raw_sort.startswith("-"):
        sort_field = raw_sort[1:] or "created_at"
        sort_dir = -1
    else:
        sort_field = raw_sort or "created_at"
        sort_dir = 1
    if sort_field not in ("created_at", "updated_at", "email", "full_name", "last_login_at"):
        sort_field = "created_at"

    db = get_db()

    # Build filter carefully (avoid clobbering search $or)
    and_parts: list[dict] = []
    if search:
        q = re.escape(search.strip())[:100]
        and_parts.append(
            {
                "$or": [
                    {"email": {"$regex": q, "$options": "i"}},
                    {"full_name": {"$regex": q, "$options": "i"}},
                    {"company_name": {"$regex": q, "$options": "i"}},
                ]
            }
        )
    if role:
        if role not in _ALLOWED_ROLES:
            raise HTTPException(status_code=400, detail="Invalid role filter")
        and_parts.append({"role": role})
    if active is not None and str(active).strip() != "":
        a = str(active).strip().lower()
        if a in ("1", "true", "yes"):
            and_parts.append({"banned": {"$ne": True}, "active": {"$ne": False}})
        elif a in ("0", "false", "no"):
            and_parts.append({"$or": [{"banned": True}, {"active": False}]})
    cf, ct = _parse_dt(created_from), _parse_dt(created_to)
    if cf or ct:
        rng: dict = {}
        if cf:
            rng["$gte"] = cf
        if ct:
            rng["$lte"] = ct
        and_parts.append({"created_at": rng})
    filt = {"$and": and_parts} if and_parts else {}

    total = await db.users.count_documents(filt)

    # Legacy offset/limit still returns a bare list for older clients
    if limit is not None or offset is not None:
        skip = int(offset or 0)
        lim = int(limit or 100)
        cur = (
            db.users.find(filt, {"password_hash": 0})
            .sort(sort_field, sort_dir)
            .skip(skip)
            .limit(lim)
        )
        return [_safe_user(u) async for u in cur]

    page_size = min(100, max(1, int(page_size)))
    page = max(1, int(page))
    skip = (page - 1) * page_size
    items = [
        _safe_user(u)
        async for u in db.users.find(filt, {"password_hash": 0})
        .sort(sort_field, sort_dir)
        .skip(skip)
        .limit(page_size)
    ]
    total_pages = math.ceil(total / page_size) if total else 0
    return {
        "items": items,
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_next": page < total_pages,
        "has_previous": page > 1 and total > 0,
    }


@router.get("/users/{uid}")
async def get_user(uid: str, admin: dict = Depends(current_admin)) -> dict:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    doc = await get_db().users.find_one({"_id": ObjectId(uid)}, {"password_hash": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return _safe_user(doc)


@router.post("/users", status_code=201)
async def create_user(body: AdminUserCreate, admin: dict = Depends(current_admin)) -> dict:
    require_permission(admin, "manage_users")
    validate_password_complexity(body.password)
    if body.role not in _ALLOWED_ROLES:
        raise HTTPException(status_code=400, detail="Unsupported role")
    # Moderators cannot create admins — only admins hit this route, still guard escalation
    if body.role == "admin" and admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Cannot assign admin role")
    plan = (body.plan or "free").strip().lower()
    if plan not in _ALLOWED_PLANS:
        raise HTTPException(status_code=400, detail="Invalid plan")
    db = get_db()
    now = utcnow()
    doc = {
        "email": body.email.lower(),
        "password_hash": hash_password(body.password),
        "full_name": body.full_name.strip(),
        "display_name": body.full_name.strip(),
        "company_name": (body.company_name or "").strip() or None,
        "phone": (body.phone or "").strip() or None,
        "plan": plan,
        "role": body.role,
        "banned": False,
        "active": bool(body.active),
        "timezone": "UTC",
        "locale": "en",
        "notification_preferences": {
            "insights": True,
            "directMsg": True,
            "maintenance": False,
        },
        "created_at": now,
        "updated_at": now,
    }
    try:
        res = await db.users.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Email already registered") from None
    doc["_id"] = res.inserted_id
    audit(
        "admin.create_user",
        user_id=str(admin["_id"]),
        target_id=str(res.inserted_id),
        request_id=get_request_id(),
        extra={"role": body.role},
    )
    await record_activity(
        db,
        tenant_id=str(admin["_id"]),
        event_type="admin.user_created",
        summary=f"User created: {body.email.lower()}",
        actor_id=str(admin["_id"]),
        resource_type="user",
        resource_id=str(res.inserted_id),
    )
    return _safe_user(doc)


@router.patch("/users/{uid}")
async def patch_user(
    uid: str, body: AdminUserPatch, admin: dict = Depends(current_admin)
) -> dict:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    raw = body.model_dump(exclude_unset=True)
    data = {k: v for k, v in raw.items() if k in _ADMIN_PATCH_ALLOW}
    if not data:
        raise HTTPException(status_code=400, detail="No valid fields to update")
    db = get_db()
    target = await db.users.find_one({"_id": ObjectId(uid)})
    if not target:
        raise HTTPException(status_code=404, detail="Not found")

    if "role" in data:
        if data["role"] not in _ALLOWED_ROLES:
            raise HTTPException(status_code=400, detail="Unsupported role")
        if data["role"] == "admin" and admin.get("role") != "admin":
            raise HTTPException(status_code=403, detail="Cannot assign admin role")
        if target.get("role") == "admin" and data["role"] != "admin":
            if await _active_admin_count(db) <= 1:
                raise HTTPException(status_code=400, detail="Cannot demote the last admin")

    if "active" in data and data["active"] is False:
        if str(admin["_id"]) == uid:
            raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
        if target.get("role") == "admin" and await _active_admin_count(db) <= 1:
            raise HTTPException(status_code=400, detail="Cannot deactivate the last admin")
        data["banned"] = True  # keep legacy ban flag in sync

    if "active" in data and data["active"] is True:
        data["banned"] = False

    if "email" in data and data["email"]:
        data["email"] = str(data["email"]).lower().strip()

    data["updated_at"] = utcnow()
    try:
        await db.users.update_one({"_id": ObjectId(uid)}, {"$set": data})
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Email already registered") from None
    audit(
        "admin.patch_user",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
        extra={"fields": sorted(k for k in data if k != "updated_at")},
    )
    fresh = await db.users.find_one({"_id": ObjectId(uid)}, {"password_hash": 0})
    return _safe_user(fresh or target)


@router.post("/users/{uid}/activate")
async def activate_user(uid: str, admin: dict = Depends(current_admin)) -> dict:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    res = await get_db().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"active": True, "banned": False, "updated_at": utcnow()}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    audit(
        "admin.activate_user",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
    )
    return {"ok": True}


@router.post("/users/{uid}/deactivate")
async def deactivate_user(uid: str, admin: dict = Depends(current_admin)) -> dict:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    if str(admin["_id"]) == uid:
        raise HTTPException(status_code=400, detail="Cannot deactivate yourself")
    db = get_db()
    target = await db.users.find_one({"_id": ObjectId(uid)})
    if not target:
        raise HTTPException(status_code=404, detail="Not found")
    if target.get("role") == "admin" and await _active_admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="Cannot deactivate the last admin")
    await db.users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"active": False, "banned": True, "updated_at": utcnow()}},
    )
    audit(
        "admin.deactivate_user",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
    )
    return {"ok": True}


@router.post("/users/{uid}/reset-password")
async def admin_reset_password(uid: str, admin: dict = Depends(current_admin)) -> dict:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    db = get_db()
    target = await db.users.find_one({"_id": ObjectId(uid)})
    if not target:
        raise HTTPException(status_code=404, detail="Not found")
    meta = await issue_reset_token(
        db,
        user_id=uid,
        email=target["email"],
        full_name=target.get("full_name") or "",
    )
    audit(
        "admin.reset_password",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
    )
    out = {"ok": True, "message": "Password reset email sent if configured."}
    # Dev/test only
    from app.config import settings

    if settings.is_dev_or_test:
        out.update({k: v for k, v in meta.items() if k.startswith("dev_")})
    return out


@router.post("/users/{uid}/ban")
async def ban_user(uid: str, admin: dict = Depends(current_admin)) -> dict:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    if str(admin["_id"]) == uid:
        raise HTTPException(status_code=400, detail="Cannot ban yourself")
    db = get_db()
    target = await db.users.find_one({"_id": ObjectId(uid)})
    if not target:
        raise HTTPException(status_code=404, detail="Not found")
    if target.get("role") == "admin" and await _active_admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="Cannot ban the last admin")
    await db.users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"banned": True, "active": False, "updated_at": utcnow()}},
    )
    audit(
        "admin.ban_user",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
    )
    return {"ok": True}


@router.post("/users/{uid}/unban")
async def unban_user(uid: str, admin: dict = Depends(current_admin)) -> dict:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    res = await get_db().users.update_one(
        {"_id": ObjectId(uid)},
        {"$set": {"banned": False, "active": True, "updated_at": utcnow()}},
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    audit(
        "admin.unban_user",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
    )
    return {"ok": True}


@router.delete("/users/{uid}", status_code=204, response_class=Response)
async def delete_user(uid: str, admin: dict = Depends(current_admin)) -> Response:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    if str(admin["_id"]) == uid:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db = get_db()
    existing = await db.users.find_one({"_id": ObjectId(uid)}, {"password_hash": 0})
    if not existing:
        raise HTTPException(status_code=404, detail="Not found")
    if existing.get("role") == "admin" and await _active_admin_count(db) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last admin")
    blast_ids = [
        str(doc["_id"])
        async for doc in db.blast_campaigns.find({"user_id": uid}, {"_id": 1})
    ]
    await db.users.delete_one({"_id": ObjectId(uid)})
    delete_credentials_for_user(user_id=uid)
    await db.leads.delete_many({"user_id": uid})
    await db.messages.delete_many({"user_id": uid})
    await db.agents.delete_many({"user_id": uid})
    await db.campaigns.delete_many({"user_id": uid})
    await db.campaign_recipients.delete_many({"user_id": uid})
    if blast_ids:
        await db.blast_recipients.delete_many({"blast_id": {"$in": blast_ids}})
    await db.blast_campaigns.delete_many({"user_id": uid})
    await db.blacklist.delete_many({"user_id": uid})
    await db.templates.delete_many({"user_id": uid})
    await db.consent_events.delete_many({"user_id": uid})
    await db.notifications.delete_many({"user_id": uid})
    await db.activity_events.delete_many({"tenant_id": uid})
    await db.password_reset_tokens.delete_many({"user_id": uid})
    await db.conversation_summaries.delete_many({"tenant_id": uid})
    await db.ai_suggestions.delete_many({"tenant_id": uid})
    await db.ai_usage.delete_many({"tenant_id": uid})
    await db.ai_events.delete_many({"tenant_id": uid})
    audit(
        "admin.delete_user",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
    )
    return Response(status_code=204)


@router.post("/users/{uid}/role")
async def update_role(uid: str, body: RoleBody, admin: dict = Depends(current_admin)) -> dict:
    require_permission(admin, "manage_users")
    require_object_id(uid)
    target = await get_db().users.find_one({"_id": ObjectId(uid)})
    if not target:
        raise HTTPException(status_code=404, detail="Not found")
    if body.role == "admin" and admin.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Cannot assign admin role")
    if target.get("role") == "admin" and body.role != "admin":
        if await _active_admin_count(get_db()) <= 1:
            raise HTTPException(status_code=400, detail="Cannot demote the last admin")
    await get_db().users.update_one(
        {"_id": ObjectId(uid)}, {"$set": {"role": body.role, "updated_at": utcnow()}}
    )
    audit(
        "admin.update_role",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
        extra={"role": body.role},
    )
    return {"ok": True}


@router.post("/users/{uid}/plan")
async def update_plan(uid: str, body: PlanBody, admin: dict = Depends(current_admin)) -> dict:
    """
    Admin plan override.

    Stripe-backed paid plans (starter/professional/business) require
    manual_override=true and do NOT create Stripe subscriptions.
    Prefer Customer Portal / Checkout for real billing changes.
    """
    from app.billing.plans import PAID_PLAN_KEYS
    from app.models.common import utcnow as _utcnow

    require_permission(admin, "manage_users")
    require_object_id(uid)
    plan = body.plan
    set_fields: dict = {"plan": plan, "updated_at": _utcnow()}

    if plan in PAID_PLAN_KEYS:
        if not body.manual_override:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Setting a Stripe-backed paid plan requires manual_override=true. "
                    "This does not create a Stripe subscription — use Checkout/Portal for paid billing."
                ),
            )
        set_fields["subscription_status"] = "manual_override"
        set_fields["subscription_updated_at"] = _utcnow()
    elif plan == "free":
        # Clearing to free without touching Stripe IDs (audit trail preserved)
        set_fields["subscription_status"] = "none"

    res = await get_db().users.update_one({"_id": ObjectId(uid)}, {"$set": set_fields})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    audit(
        "admin.update_plan",
        user_id=str(admin["_id"]),
        target_id=uid,
        request_id=get_request_id(),
        extra={
            "plan": plan,
            "manual_override": bool(body.manual_override),
        },
    )
    return {"ok": True, "manual_override": bool(body.manual_override and plan in PAID_PLAN_KEYS)}
