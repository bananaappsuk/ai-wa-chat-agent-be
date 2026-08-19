"""Tenant Meta Cloud API credentials (encrypted at rest). Never log or return plaintext."""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pymongo import MongoClient

from app.config import settings
from app.models.common import utcnow

logger = logging.getLogger(__name__)

ALG_AESGCM = "AESGCM"
KEY_ID_DEFAULT = "v1"
CONNECTED_STATUSES = frozenset({"connected", "legacy_poc"})

_sync_client: MongoClient | None = None


class MetaCredentialsError(Exception):
    """Tenant Meta credentials missing, invalid, or not permitted."""


def _sync_db():
    global _sync_client
    if _sync_client is None:
        _sync_client = MongoClient(settings.MONGO_URI)
    return _sync_client[settings.MONGO_DB]


def _coll(db=None):
    return (db if db is not None else _sync_db()).meta_credentials


def load_encryption_key(raw: Optional[str] = None) -> bytes:
    text = (raw if raw is not None else settings.META_TOKEN_ENCRYPTION_KEY) or ""
    text = str(text).strip()
    if not text:
        raise MetaCredentialsError("META_TOKEN_ENCRYPTION_KEY is not configured")
    jwt = (settings.JWT_SECRET or "").strip()
    if jwt and text == jwt:
        raise MetaCredentialsError("META_TOKEN_ENCRYPTION_KEY must not equal JWT_SECRET")
    if text.startswith("base64:"):
        blob = base64.urlsafe_b64decode(text[7:] + "==")
    else:
        try:
            blob = base64.urlsafe_b64decode(text + "==")
            if len(blob) not in (16, 24, 32):
                blob = text.encode("utf-8")
        except Exception:
            blob = text.encode("utf-8")
    if len(blob) not in (16, 24, 32):
        raise MetaCredentialsError("META_TOKEN_ENCRYPTION_KEY must decode to 16, 24, or 32 bytes")
    return blob


def encrypt_secret(plaintext: str, *, key: Optional[bytes] = None) -> str:
    token = (plaintext or "")
    if not token:
        raise MetaCredentialsError("Cannot encrypt empty secret")
    aes_key = key if key is not None else load_encryption_key()
    nonce = os.urandom(12)
    aes = AESGCM(aes_key)
    packed = aes.encrypt(nonce, token.encode("utf-8"), None)
    return "v1." + base64.urlsafe_b64encode(nonce + packed).decode("ascii")


def decrypt_secret(ciphertext: str, *, key: Optional[bytes] = None) -> str:
    raw = (ciphertext or "").strip()
    if not raw.startswith("v1."):
        raise MetaCredentialsError("Unsupported ciphertext format")
    try:
        blob = base64.urlsafe_b64decode(raw[3:] + "==")
    except Exception as exc:
        raise MetaCredentialsError("Invalid ciphertext") from exc
    if len(blob) < 13:
        raise MetaCredentialsError("Invalid ciphertext")
    nonce, packed = blob[:12], blob[12:]
    aes_key = key if key is not None else load_encryption_key()
    try:
        return AESGCM(aes_key).decrypt(nonce, packed, None).decode("utf-8")
    except InvalidTag as exc:
        raise MetaCredentialsError("Ciphertext authentication failed") from exc
    except Exception as exc:
        raise MetaCredentialsError("Decrypt failed") from exc


def _user_id(user: Optional[dict]) -> str:
    if not user:
        return ""
    return str(user.get("_id") or "").strip()


def _pnid(user: Optional[dict]) -> str:
    return str((user or {}).get("meta_phone_number_id") or "").strip()


def _status(user: Optional[dict]) -> str:
    return str((user or {}).get("meta_connection_status") or "").strip().lower()


def production_blocks_legacy_flag() -> bool:
    return bool(settings.is_production_like and settings.META_ALLOW_LEGACY_POC_TOKEN)


def legacy_poc_fallback_allowed(user: Optional[dict]) -> bool:
    """Env token is allowed only in dev/test with an explicit flag and matching PNID."""
    if production_blocks_legacy_flag():
        return False
    if settings.is_production_like:
        return False
    if not settings.META_ALLOW_LEGACY_POC_TOKEN:
        return False
    if not settings.is_dev_or_test:
        return False
    if _status(user) != "legacy_poc":
        return False
    env_pnid = (settings.META_PHONE_NUMBER_ID or "").strip()
    env_token = (settings.META_ACCESS_TOKEN or "").strip()
    if not env_pnid or not env_token:
        return False
    return bool(_pnid(user) and _pnid(user) == env_pnid)


@dataclass(frozen=True)
class MetaTenantCredentials:
    access_token: str
    phone_number_id: str
    waba_id: str


def upsert_encrypted_access_token(
    *,
    user_id: str,
    access_token: str,
    phone_number_id: str,
    db=None,
    expires_at: Optional[datetime] = None,
    scopes: Optional[list[str]] = None,
) -> dict[str, Any]:
    uid = str(user_id or "").strip()
    pnid = str(phone_number_id or "").strip()
    token = str(access_token or "").strip()
    if not uid or not pnid or not token:
        raise MetaCredentialsError("user_id, phone_number_id, and access_token are required")
    payload = json.dumps({"t": token, "p": pnid}, separators=(",", ":"))
    now = utcnow()
    doc = {
        "user_id": uid,
        "ciphertext": encrypt_secret(payload),
        "algorithm": ALG_AESGCM,
        "key_id": KEY_ID_DEFAULT,
        "expires_at": expires_at,
        "scopes": scopes,
        "updated_at": now,
    }
    coll = _coll(db)
    existing = coll.find_one({"user_id": uid})
    if existing:
        coll.update_one({"user_id": uid}, {"$set": doc})
        return {"user_id": uid, "created": False}
    doc["created_at"] = now
    coll.insert_one(doc)
    return {"user_id": uid, "created": True}


def delete_credentials_for_user(*, user_id: str, db=None) -> None:
    uid = str(user_id or "").strip()
    if uid:
        _coll(db).delete_one({"user_id": uid})


def get_meta_credentials_for_user(user: Optional[dict], *, db=None) -> MetaTenantCredentials:
    """Return tenant Graph credentials. Never use env token unless legacy_poc policy matches."""
    if production_blocks_legacy_flag():
        raise MetaCredentialsError("META_ALLOW_LEGACY_POC_TOKEN is not allowed in this environment")
    if not user:
        raise MetaCredentialsError("Meta credentials require a tenant user")
    pnid = _pnid(user)
    if not pnid:
        raise MetaCredentialsError("Meta phone number ID is not linked to this account")
    status = _status(user)
    waba = str(user.get("meta_waba_id") or "").strip()

    if status == "connected":
        creds = _load_encrypted(user, expected_pnid=pnid, db=db)
        return MetaTenantCredentials(
            access_token=creds["access_token"],
            phone_number_id=pnid,
            waba_id=waba,
        )

    if status == "legacy_poc":
        if legacy_poc_fallback_allowed(user):
            return MetaTenantCredentials(
                access_token=(settings.META_ACCESS_TOKEN or "").strip(),
                phone_number_id=pnid,
                waba_id=waba or (settings.META_WABA_ID or "").strip(),
            )
        # Encrypted row may exist after explicit migration even in legacy_poc.
        creds = _load_encrypted(user, expected_pnid=pnid, db=db)
        return MetaTenantCredentials(
            access_token=creds["access_token"],
            phone_number_id=pnid,
            waba_id=waba or (settings.META_WABA_ID or "").strip(),
        )

    raise MetaCredentialsError("Meta WhatsApp is not connected for this account")


def _load_encrypted(user: dict, *, expected_pnid: str, db=None) -> dict[str, str]:
    uid = _user_id(user)
    row = _coll(db).find_one({"user_id": uid})
    if not row:
        raise MetaCredentialsError("Meta credentials are not configured for this account")
    payload = decrypt_secret(str(row.get("ciphertext") or ""))
    try:
        data = json.loads(payload)
    except Exception as exc:
        raise MetaCredentialsError("Invalid credential payload") from exc
    token = str((data or {}).get("t") or "").strip()
    bound = str((data or {}).get("p") or "").strip()
    if not token:
        raise MetaCredentialsError("Meta credentials are empty")
    if bound != expected_pnid:
        raise MetaCredentialsError("Meta credential phone number ID does not match this account")
    return {"access_token": token, "phone_number_id": bound}


def tenant_meta_ready(user: Optional[dict], *, db=None) -> bool:
    try:
        get_meta_credentials_for_user(user, db=db)
        return True
    except MetaCredentialsError:
        return False


def migrate_legacy_poc_user(user: dict, *, dry_run: bool = True, db=None) -> dict[str, Any]:
    """Encrypt env META_ACCESS_TOKEN for a user whose PNID matches env. Not run at startup."""
    env_pnid = (settings.META_PHONE_NUMBER_ID or "").strip()
    env_token = (settings.META_ACCESS_TOKEN or "").strip()
    env_waba = (settings.META_WABA_ID or "").strip()
    pnid = _pnid(user)
    uid = _user_id(user)
    out = {
        "dry_run": dry_run,
        "user_id": uid,
        "eligible": False,
        "updated": False,
        "reason": None,
    }
    if not uid or not pnid or pnid != env_pnid or not env_token:
        out["reason"] = "user PNID does not match env META_PHONE_NUMBER_ID or token missing"
        return out
    out["eligible"] = True
    if dry_run:
        out["reason"] = "dry_run"
        return out
    upsert_encrypted_access_token(
        user_id=uid,
        access_token=env_token,
        phone_number_id=pnid,
        db=db,
    )
    users = (db if db is not None else _sync_db()).users
    set_doc: dict[str, Any] = {
        "meta_connection_status": "legacy_poc",
        "meta_onboarding_source": "legacy_poc",
        "updated_at": utcnow(),
    }
    if env_waba and not str(user.get("meta_waba_id") or "").strip():
        set_doc["meta_waba_id"] = env_waba
    if not user.get("meta_connected_at"):
        set_doc["meta_connected_at"] = utcnow()
    users.update_one({"_id": user["_id"]}, {"$set": set_doc})
    out["updated"] = True
    return out
