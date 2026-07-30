from fastapi import APIRouter, HTTPException, Request
from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.billing.user_out import user_out_from_doc
from app.config import settings
from app.db.mongo import get_db
from app.models.user import (
    UserCreate,
    UserLogin,
    TokenOut,
    UserOut,
    ForgotPasswordBody,
    ResetPasswordBody,
    ChangePasswordBody,
)
from app.models.common import utcnow, serialize
from app.middleware.auth import (
    hash_password,
    verify_password,
    create_access_token,
    current_user,
)
from app.middleware.security import get_request_id
from app.security.audit import audit
from app.security.rate_limit import rate_limit_auth, rate_limit_ip, rate_limit_user
from app.security.validation import validate_password_complexity
from app.services.password_reset import issue_reset_token, consume_reset_token
from app.services.activity import record_activity
from fastapi import Depends

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(doc: dict) -> UserOut:
    return user_out_from_doc(doc)


def _rate_password(request: Request) -> None:
    rate_limit_ip(
        request,
        bucket="password_reset",
        limit=max(1, int(settings.PASSWORD_RESET_RATE_LIMIT)),
        window_sec=60,
    )


@router.post("/signup", response_model=TokenOut)
async def signup(payload: UserCreate, request: Request) -> TokenOut:
    rate_limit_auth(request)
    validate_password_complexity(payload.password)
    db = get_db()
    if await db.users.find_one({"email": payload.email.lower()}):
        raise HTTPException(status_code=409, detail="Email already registered")
    is_first = (await db.users.estimated_document_count()) == 0
    doc = {
        "email": payload.email.lower(),
        "password_hash": hash_password(payload.password),
        "full_name": payload.full_name.strip(),
        "first_name": None,
        "last_name": None,
        "display_name": payload.full_name.strip(),
        "company_name": (payload.company_name or "").strip() or None,
        "phone": (payload.phone or "").strip() or None,
        "timezone": "UTC",
        "locale": "en",
        "notification_preferences": {
            "insights": True,
            "directMsg": True,
            "maintenance": False,
        },
        "plan": "free",
        "subscription_status": "none",
        "cancel_at_period_end": False,
        "role": "admin" if is_first else "user",
        "banned": False,
        "active": True,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    try:
        res = await db.users.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="Email already registered") from None
    doc["_id"] = res.inserted_id
    token = create_access_token(str(res.inserted_id), doc["role"])
    audit(
        "auth.signup",
        user_id=str(res.inserted_id),
        result="ok",
        request_id=get_request_id(),
    )
    await record_activity(
        db,
        tenant_id=str(res.inserted_id),
        event_type="user.signup",
        summary="Account created",
        actor_id=str(res.inserted_id),
        resource_type="user",
        resource_id=str(res.inserted_id),
    )
    return TokenOut(access_token=token, user=_user_out(doc))


@router.post("/login", response_model=TokenOut)
async def login(payload: UserLogin, request: Request) -> TokenOut:
    rate_limit_auth(request)
    db = get_db()
    user = await db.users.find_one({"email": payload.email.lower()})
    if not user or not verify_password(payload.password, user.get("password_hash", "")):
        audit(
            "auth.login",
            user_id=str(user["_id"]) if user else None,
            result="failure",
            request_id=get_request_id(),
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if user.get("banned") or user.get("active") is False:
        audit(
            "auth.login",
            user_id=str(user["_id"]),
            result="banned",
            request_id=get_request_id(),
        )
        raise HTTPException(status_code=403, detail="Invalid credentials")
    await db.users.update_one(
        {"_id": user["_id"]},
        {"$set": {"last_login_at": utcnow(), "updated_at": utcnow()}},
    )
    token = create_access_token(str(user["_id"]), user.get("role", "user"))
    audit(
        "auth.login",
        user_id=str(user["_id"]),
        result="ok",
        request_id=get_request_id(),
    )
    await record_activity(
        db,
        tenant_id=str(user["_id"]),
        event_type="user.login",
        summary="User signed in",
        actor_id=str(user["_id"]),
        resource_type="user",
        resource_id=str(user["_id"]),
    )
    user["last_login_at"] = utcnow()
    return TokenOut(access_token=token, user=_user_out(user))


@router.post("/forgot-password")
async def forgot_password(payload: ForgotPasswordBody, request: Request) -> dict:
    """Always returns a generic message — no account enumeration."""
    _rate_password(request)
    rate_limit_auth(request)
    db = get_db()
    email = payload.email.lower().strip()
    user = await db.users.find_one({"email": email})
    if user and user.get("active") is not False and not user.get("banned"):
        await issue_reset_token(
            db,
            user_id=str(user["_id"]),
            email=email,
            full_name=user.get("full_name") or "",
        )
        audit(
            "auth.forgot_password",
            user_id=str(user["_id"]),
            result="ok",
            request_id=get_request_id(),
        )
    else:
        audit(
            "auth.forgot_password",
            result="ok",
            request_id=get_request_id(),
            extra={"email_known": False},
        )
    # Generic response always — never reveal whether email exists
    return {
        "ok": True,
        "message": "If an account exists for that email, a reset link has been sent.",
    }

@router.post("/forgot-password/dev-issue")
async def forgot_password_dev_issue(payload: ForgotPasswordBody, request: Request) -> dict:
    """Dev/test only: issue reset token and return it (never in staging/production)."""
    if not settings.is_dev_or_test:
        raise HTTPException(status_code=404, detail="Not found")
    _rate_password(request)
    db = get_db()
    user = await db.users.find_one({"email": payload.email.lower()})
    if not user:
        return {"ok": True, "message": "If an account exists for that email, a reset link has been sent."}
    meta = await issue_reset_token(
        db,
        user_id=str(user["_id"]),
        email=user["email"],
        full_name=user.get("full_name") or "",
    )
    return {
        "ok": True,
        "message": "If an account exists for that email, a reset link has been sent.",
        **{k: v for k, v in meta.items() if k.startswith("dev_")},
    }


@router.post("/reset-password")
async def reset_password(payload: ResetPasswordBody, request: Request) -> dict:
    _rate_password(request)
    validate_password_complexity(payload.new_password)
    db = get_db()
    doc = await consume_reset_token(db, raw_token=payload.token)
    if not doc:
        audit("auth.reset_password", result="failure", request_id=get_request_id())
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user_id = doc["user_id"]
    now = utcnow()
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "password_hash": hash_password(payload.new_password),
                "password_changed_at": now,
                "updated_at": now,
            }
        },
    )
    # Invalidate any other outstanding tokens
    await db.password_reset_tokens.update_many(
        {"user_id": user_id, "used_at": None},
        {"$set": {"used_at": now, "invalidated": True}},
    )
    audit(
        "auth.reset_password",
        user_id=user_id,
        result="ok",
        request_id=get_request_id(),
    )
    await record_activity(
        db,
        tenant_id=user_id,
        event_type="user.password_reset",
        summary="Password reset completed",
        actor_id=user_id,
        resource_type="user",
        resource_id=user_id,
    )
    from app.services.notifications import create_notification

    await create_notification(
        db,
        user_id=user_id,
        type="security_notice",
        title="Password changed",
        message="Your password was reset successfully.",
        resource_type="user",
        resource_id=user_id,
        dedupe_key=f"pwdreset:{user_id}:{int(now.timestamp())}",
    )
    return {"ok": True, "message": "Password updated. You can sign in with your new password."}


@router.post("/change-password")
async def change_password(
    payload: ChangePasswordBody,
    request: Request,
    user: dict = Depends(current_user),
) -> dict:
    rate_limit_user(
        str(user["_id"]),
        bucket="change_password",
        limit=max(1, int(settings.PASSWORD_RESET_RATE_LIMIT)),
        window_sec=60,
    )
    validate_password_complexity(payload.new_password)
    if not verify_password(payload.current_password, user.get("password_hash", "")):
        audit(
            "auth.change_password",
            user_id=str(user["_id"]),
            result="failure",
            request_id=get_request_id(),
        )
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    now = utcnow()
    await get_db().users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_hash": hash_password(payload.new_password),
                "password_changed_at": now,
                "updated_at": now,
            }
        },
    )
    audit(
        "auth.change_password",
        user_id=str(user["_id"]),
        result="ok",
        request_id=get_request_id(),
    )
    await record_activity(
        get_db(),
        tenant_id=str(user["_id"]),
        event_type="user.password_changed",
        summary="Password changed",
        actor_id=str(user["_id"]),
        resource_type="user",
        resource_id=str(user["_id"]),
    )
    return {"ok": True, "message": "Password updated"}


@router.get("/me", response_model=UserOut)
async def me(user_id: str | None = None):
    raise HTTPException(status_code=404, detail="Use /api/profile/me")
