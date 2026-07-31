"""Password reset token lifecycle (hashed, one-time, expiring)."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any, Optional

from bson import ObjectId

from app.config import settings
from app.models.common import utcnow
from app.services.email_service import build_password_reset_email, send_email


def _hash_token(raw: str) -> str:
    secret = (settings.JWT_SECRET or "reset").encode("utf-8")
    return hmac.new(secret, raw.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_raw_token() -> str:
    return secrets.token_urlsafe(32)


async def issue_reset_token(db, *, user_id: str, email: str, full_name: str = "") -> dict[str, Any]:
    """Invalidate prior tokens, store hashed token, optionally email. Returns safe metadata (+ raw in test)."""
    raw = generate_raw_token()
    token_hash = _hash_token(raw)
    now = utcnow()
    expire_min = max(5, int(settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES))
    expires_at = now + timedelta(minutes=expire_min)

    await db.password_reset_tokens.update_many(
        {"user_id": user_id, "used_at": None},
        {"$set": {"used_at": now, "invalidated": True}},
    )
    await db.password_reset_tokens.insert_one(
        {
            "user_id": user_id,
            "email": email.lower(),
            "token_hash": token_hash,
            "created_at": now,
            "expires_at": expires_at,
            "used_at": None,
            "invalidated": False,
        }
    )

    base = (settings.PASSWORD_RESET_FRONTEND_URL or "").strip().rstrip("/")
    reset_url = f"{base}?token={raw}" if base else f"/reset-password?token={raw}"
    subject, text, html = build_password_reset_email(reset_url=reset_url, user_name=full_name)
    send_email(to=email, subject=subject, text_body=text, html_body=html)

    out: dict[str, Any] = {"ok": True, "expires_at": expires_at.isoformat()}
    # Dev/test only — never expose in production-like envs
    if settings.is_dev_or_test:
        out["dev_reset_token"] = raw
        out["dev_reset_url"] = reset_url
    return out


async def consume_reset_token(db, *, raw_token: str) -> Optional[dict]:
    """Validate and mark token used. Returns token doc or None."""
    if not raw_token or len(raw_token) < 20 or len(raw_token) > 200:
        return None
    token_hash = _hash_token(raw_token.strip())
    now = utcnow()
    doc = await db.password_reset_tokens.find_one(
        {"token_hash": token_hash, "used_at": None, "invalidated": {"$ne": True}}
    )
    if not doc:
        return None
    exp = doc.get("expires_at")
    if exp and getattr(exp, "tzinfo", None) is None:
        from datetime import timezone

        exp = exp.replace(tzinfo=timezone.utc)
    if not exp or exp < now:
        return None
    await db.password_reset_tokens.update_one(
        {"_id": doc["_id"]},
        {"$set": {"used_at": now}},
    )
    return doc
