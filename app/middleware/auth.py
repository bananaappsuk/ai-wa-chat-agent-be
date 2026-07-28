from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import ExpiredSignatureError, JWTError, jwt
from passlib.context import CryptContext

from app.config import settings
from app.db.mongo import get_db
from app.security.validation import require_object_id

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer = HTTPBearer(auto_error=False)

_ALLOWED_ALGS = ("HS256", "HS384", "HS512")


def hash_password(p: str) -> str:
    return pwd_ctx.hash(p)


def verify_password(p: str, h: str) -> bool:
    if not h:
        return False
    try:
        return pwd_ctx.verify(p, h)
    except Exception:
        return False


def create_access_token(sub: str, role: str = "user") -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=max(1, int(settings.JWT_EXPIRE_MIN)))
    payload = {
        "sub": str(sub),
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "typ": "access",
    }
    alg = settings.JWT_ALG if settings.JWT_ALG in _ALLOWED_ALGS else "HS256"
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=alg)


def decode_token(token: str) -> dict:
    if not token or not isinstance(token, str) or token.count(".") != 2:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    alg = settings.JWT_ALG if settings.JWT_ALG in _ALLOWED_ALGS else "HS256"
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[alg],
            options={
                "require_exp": True,
                "require_sub": True,
                "verify_aud": False,
            },
        )
    except ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    if not isinstance(payload, dict):
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    typ = payload.get("typ")
    if typ is not None and typ != "access":
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return payload


async def _user_from_token(token: str) -> dict:
    payload = decode_token(token)
    uid = payload.get("sub")
    if not uid or not ObjectId.is_valid(str(uid)):
        raise HTTPException(status_code=401, detail="Invalid token subject")
    user = await get_db().users.find_one({"_id": ObjectId(str(uid))})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    if user.get("banned") or user.get("active") is False:
        raise HTTPException(status_code=403, detail="User disabled")
    # Revoke tokens issued before password change
    pca = user.get("password_changed_at")
    if pca is not None:
        iat = payload.get("iat")
        try:
            if isinstance(iat, (int, float)):
                iat_dt = datetime.fromtimestamp(iat, tz=timezone.utc)
            elif isinstance(iat, datetime):
                iat_dt = iat if iat.tzinfo else iat.replace(tzinfo=timezone.utc)
            else:
                iat_dt = None
            if isinstance(pca, datetime):
                pca_dt = pca if pca.tzinfo else pca.replace(tzinfo=timezone.utc)
                if iat_dt and iat_dt < pca_dt:
                    raise HTTPException(status_code=401, detail="Invalid or expired token")
        except HTTPException:
            raise
        except Exception:
            pass
    return user


async def current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
) -> dict:
    if not creds or not creds.credentials:
        raise HTTPException(status_code=401, detail="Missing token")
    return await _user_from_token(creds.credentials)


async def current_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user


# Aliases required by security hardening brief
require_authenticated_user = current_user
require_admin = current_admin


async def require_tenant_resource(
    *,
    collection: str,
    resource_id: str,
    user: dict,
    not_found_detail: str = "Not found",
) -> dict:
    """Load a document owned by the authenticated tenant or raise 404."""
    oid = require_object_id(resource_id, detail=not_found_detail)
    doc = await get_db()[collection].find_one({"_id": oid, "user_id": str(user["_id"])})
    if not doc:
        raise HTTPException(status_code=404, detail=not_found_detail)
    return doc


async def ws_user(token: str) -> Optional[dict]:
    if not token:
        return None
    try:
        return await _user_from_token(token)
    except HTTPException:
        return None
