from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.db.mongo import get_db
from app.middleware.auth import current_user, verify_password
from app.middleware.security import get_request_id
from app.models.common import serialize, utcnow
from app.models.user import ProfileUpdate, UserOut
from app.billing.user_out import user_out_from_doc
from app.security.audit import audit
from app.security.rate_limit import rate_limit_upload
from app.services import twilio_service
from app.services.activity import record_activity
from app.services.lead_service import _norm_phone
from app.services.media_storage import get_media_storage
from app.services.notifications import create_notification

router = APIRouter(prefix="/profile", tags=["profile"])

_AVATAR_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/jpg"})
_PROFILE_ALLOW = frozenset(
    {
        "full_name",
        "first_name",
        "last_name",
        "display_name",
        "company_name",
        "phone",
        "twilio_whatsapp_to",
        "timezone",
        "locale",
        "notification_preferences",
        "email",
        "current_password",
    }
)


def _user_out(doc: dict) -> UserOut:
    return user_out_from_doc(doc)


def normalize_twilio_whatsapp_to(raw: str | None) -> str | None:
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    phone = twilio_service.from_whatsapp(stripped)
    normalized = _norm_phone(phone)
    if not normalized:
        return None
    digits = normalized[1:] if normalized.startswith("+") else normalized
    if not normalized.startswith("+") or not digits.isdigit() or not (8 <= len(digits) <= 15):
        raise HTTPException(
            status_code=400,
            detail="Business WhatsApp number must be international format, e.g. +447700900000",
        )
    return normalized


def _validate_timezone(tz: str | None) -> str | None:
    if tz is None:
        return None
    name = tz.strip()
    if not name:
        return None
    # UTC always allowed (works without tzdata package on Windows)
    if name.upper() in ("UTC", "GMT", "ETC/UTC"):
        return "UTC" if name.upper() != "GMT" else "GMT"
    try:
        ZoneInfo(name)
        return name
    except ZoneInfoNotFoundError:
        # Accept IANA-shaped names when tzdata is unavailable (dev on Windows)
        import re

        if re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_\-+]+)+", name):
            return name
        raise HTTPException(status_code=400, detail="Invalid timezone") from None
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid timezone") from exc


def _validate_phone(phone: str | None) -> str | None:
    if phone is None:
        return None
    stripped = phone.strip()
    if not stripped:
        return None
    if len(stripped) > 20:
        raise HTTPException(status_code=400, detail="Invalid phone")
    return stripped


async def _apply_profile_update(payload: ProfileUpdate, user: dict) -> UserOut:
    raw = payload.model_dump(exclude_unset=True)
    # Explicit allow-list — reject unknown keys from model extras (none expected)
    data = {k: v for k, v in raw.items() if k in _PROFILE_ALLOW}
    unknown = set(raw) - _PROFILE_ALLOW
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unsupported fields: {', '.join(sorted(unknown))}")

    update: dict = {}
    email_changing = False
    current_password = data.pop("current_password", None)

    if "email" in data and data["email"] is not None:
        new_email = str(data["email"]).lower().strip()
        if new_email != (user.get("email") or "").lower():
            email_changing = True
            if not current_password or not verify_password(
                str(current_password), user.get("password_hash", "")
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Current password required to change email",
                )
            update["email"] = new_email

    if "timezone" in data:
        update["timezone"] = _validate_timezone(data["timezone"])
    if "phone" in data:
        update["phone"] = _validate_phone(data["phone"])
    if "twilio_whatsapp_to" in data:
        update["twilio_whatsapp_to"] = normalize_twilio_whatsapp_to(data["twilio_whatsapp_to"])
    if "locale" in data and data["locale"] is not None:
        loc = str(data["locale"]).strip()[:16]
        update["locale"] = loc or None
    if "notification_preferences" in data and data["notification_preferences"] is not None:
        prefs = data["notification_preferences"]
        if not isinstance(prefs, dict):
            raise HTTPException(status_code=400, detail="Invalid notification preferences")
        safe_prefs = {
            str(k)[:40]: bool(v)
            for k, v in prefs.items()
            if str(k) in ("insights", "directMsg", "maintenance", "campaigns", "security")
        }
        update["notification_preferences"] = {
            **(user.get("notification_preferences") or {}),
            **safe_prefs,
        }

    for key in ("full_name", "first_name", "last_name", "display_name", "company_name"):
        if key in data and data[key] is not None:
            val = str(data[key]).strip()
            update[key] = val or None

    if "full_name" in update and update["full_name"] and not update.get("display_name"):
        if "display_name" not in data:
            update.setdefault("display_name", update["full_name"])

    if not update:
        return _user_out(user)

    db = get_db()
    new_wa = update.get("twilio_whatsapp_to")
    if new_wa:
        from app.services.entitlements import require_can_connect_number

        require_can_connect_number(user)
        clash = await db.users.find_one(
            {"twilio_whatsapp_to": new_wa, "_id": {"$ne": ObjectId(user["_id"])}}
        )
        if clash:
            raise HTTPException(
                status_code=409,
                detail="That WhatsApp number is already linked to another account",
            )

    update["updated_at"] = utcnow()
    try:
        await db.users.update_one({"_id": ObjectId(user["_id"])}, {"$set": update})
    except DuplicateKeyError:
        if email_changing:
            raise HTTPException(status_code=409, detail="Email already registered") from None
        raise HTTPException(
            status_code=409,
            detail="That WhatsApp number is already linked to another account",
        ) from None

    fresh = await db.users.find_one({"_id": ObjectId(user["_id"])})
    audit(
        "profile.update",
        user_id=str(user["_id"]),
        request_id=get_request_id(),
        extra={"fields": sorted(k for k in update if k != "updated_at")},
    )
    await record_activity(
        db,
        tenant_id=str(user["_id"]),
        event_type="profile.updated",
        summary="Profile updated",
        actor_id=str(user["_id"]),
        resource_type="user",
        resource_id=str(user["_id"]),
        metadata={"fields": sorted(k for k in update if k != "updated_at")},
    )
    if email_changing:
        await create_notification(
            db,
            user_id=str(user["_id"]),
            type="security_notice",
            title="Email changed",
            message="Your account email was updated.",
            resource_type="user",
            resource_id=str(user["_id"]),
            dedupe_key=f"emailchg:{user['_id']}:{int(utcnow().timestamp())}",
        )
    return _user_out(fresh or user)


@router.get("", response_model=UserOut)
@router.get("/", response_model=UserOut, include_in_schema=False)
@router.get("/me", response_model=UserOut)
async def get_profile(user: dict = Depends(current_user)) -> UserOut:
    return _user_out(user)


@router.patch("", response_model=UserOut)
@router.patch("/", response_model=UserOut, include_in_schema=False)
@router.patch("/me", response_model=UserOut)
async def patch_profile(
    payload: ProfileUpdate, user: dict = Depends(current_user)
) -> UserOut:
    return await _apply_profile_update(payload, user)


@router.post("/avatar", response_model=UserOut)
async def upload_avatar(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> UserOut:
    user_id = str(user["_id"])
    rate_limit_upload(user_id)
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type == "image/jpg":
        content_type = "image/jpeg"
    if content_type not in _AVATAR_MIMES:
        raise HTTPException(status_code=400, detail="Avatar must be JPEG, PNG, or WebP")
    data = await file.read()
    max_bytes = max(1, int(settings.AVATAR_MAX_BYTES))
    if len(data) > max_bytes:
        raise HTTPException(status_code=400, detail="Avatar file too large")
    if len(data) < 32:
        raise HTTPException(status_code=400, detail="Invalid avatar file")

    storage = get_media_storage()
    saved = storage.save(
        user_id=user_id,
        filename=file.filename or "avatar.jpg",
        content_type=content_type,
        data=data,
    )
    url = saved.get("url") or saved.get("public_path")
    await get_db().users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "avatar_url": url,
                "avatar_storage_key": saved.get("storage_key"),
                "updated_at": utcnow(),
            }
        },
    )
    audit("profile.avatar_upload", user_id=user_id, request_id=get_request_id())
    fresh = await get_db().users.find_one({"_id": ObjectId(user_id)})
    return _user_out(fresh or user)


@router.delete("/avatar", response_model=UserOut)
async def delete_avatar(user: dict = Depends(current_user)) -> UserOut:
    user_id = str(user["_id"])
    await get_db().users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {"avatar_url": None, "avatar_storage_key": None, "updated_at": utcnow()},
        },
    )
    audit("profile.avatar_delete", user_id=user_id, request_id=get_request_id())
    fresh = await get_db().users.find_one({"_id": ObjectId(user_id)})
    return _user_out(fresh or user)
