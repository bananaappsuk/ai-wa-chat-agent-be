"""Meta Embedded Signup v4 onboarding (server-side code exchange). Never log code/token/secret.

State semantics: Redis session is bound to user_id, single-use, ~10 minute TTL.
State is NOT marked consumed until Graph verification + credential persist succeed
(or an honest error status is written). Recoverable Graph failures can retry the
same state; the Meta authorization code itself still expires in ~30s per Meta docs.
Replay of a consumed state is rejected. Tenant B cannot complete tenant A's state.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import timedelta
from typing import Any, Optional

import httpx
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from app.config import settings
from app.models.common import utcnow
from app.services.meta_credentials import (
    delete_credentials_for_user,
    restore_credentials_snapshot,
    snapshot_credentials_row,
    upsert_encrypted_access_token,
)
from app.services.whatsapp_settings import _mask_id, validate_graph_id

logger = logging.getLogger(__name__)

SESSION_PREFIX = "meta_onboard:"
INFLIGHT_PREFIX = "meta_onboard:lock:"
_ALREADY_REGISTERED = frozenset({"133010", "133016"})


class MetaOnboardingError(Exception):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _redis():
    from app.workers.queue import get_redis

    return get_redis()


def _ttl() -> int:
    return max(60, int(settings.META_ONBOARDING_STATE_TTL_SECONDS or 600))


def _graph_version() -> str:
    return (settings.META_GRAPH_VERSION or "v21.0").strip().lstrip("/")


def _timeout() -> float:
    return max(5.0, float(settings.META_HTTP_TIMEOUT_SECONDS or 30.0))


def embedded_signup_available() -> bool:
    return bool(settings.embedded_signup_available)


def start_onboarding_session(*, user_id: str) -> dict[str, str]:
    app_id = (settings.META_APP_ID or "").strip()
    config_id = (settings.META_EMBEDDED_SIGNUP_CONFIG_ID or "").strip()
    if not app_id or not config_id:
        raise HTTPException(
            status_code=400,
            detail="Meta Embedded Signup is not configured",
        )
    uid = str(user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="Invalid account")
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(16)
    now = utcnow()
    payload = {
        "user_id": uid,
        "nonce": nonce,
        "created_at": now.isoformat(),
        "consumed": False,
    }
    r = _redis()
    r.set(f"{SESSION_PREFIX}{state}", json.dumps(payload), ex=_ttl())
    return {
        "state": state,
        "app_id": app_id,
        "config_id": config_id,
        "graph_version": _graph_version(),
    }


def _load_session(state: str) -> dict[str, Any]:
    raw_state = (state or "").strip()
    if not raw_state or len(raw_state) > 128:
        raise HTTPException(status_code=400, detail="Invalid onboarding session")
    raw = _redis().get(f"{SESSION_PREFIX}{raw_state}")
    if not raw:
        raise HTTPException(status_code=400, detail="Onboarding session expired or not found")
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid onboarding session") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Invalid onboarding session")
    return data


def assert_session_for_user(*, state: str, user_id: str) -> dict[str, Any]:
    data = _load_session(state)
    if data.get("consumed") is True:
        raise HTTPException(status_code=400, detail="Onboarding session already used")
    if str(data.get("user_id") or "") != str(user_id):
        raise HTTPException(status_code=403, detail="Onboarding session does not belong to this account")
    return data


def _mark_consumed(state: str, data: dict[str, Any]) -> None:
    data = dict(data)
    data["consumed"] = True
    ttl = _redis().ttl(f"{SESSION_PREFIX}{state}")
    ex = ttl if isinstance(ttl, int) and ttl > 0 else 60
    _redis().set(f"{SESSION_PREFIX}{state}", json.dumps(data), ex=ex)


def _acquire_inflight(state: str) -> None:
    ok = _redis().set(f"{INFLIGHT_PREFIX}{state}", "1", nx=True, ex=90)
    if not ok:
        raise HTTPException(status_code=409, detail="Onboarding is already in progress")


def _release_inflight(state: str) -> None:
    try:
        _redis().delete(f"{INFLIGHT_PREFIX}{state}")
    except Exception:
        pass


def _graph_get(path: str, *, token: str, params: Optional[dict] = None) -> dict[str, Any]:
    version = _graph_version()
    url = f"https://graph.facebook.com/{version}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(timeout=_timeout()) as client:
        resp = client.get(url, headers=headers, params=params or {})
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.is_error or not isinstance(data, dict):
        raise MetaOnboardingError("Meta Graph request failed", status_code=502)
    if data.get("error"):
        raise MetaOnboardingError("Meta Graph request failed", status_code=502)
    return data


def _graph_post(path: str, *, token: str, json_body: Optional[dict] = None) -> dict[str, Any]:
    version = _graph_version()
    url = f"https://graph.facebook.com/{version}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=_timeout()) as client:
        resp = client.post(url, headers=headers, json=json_body or {})
    try:
        data = resp.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {"status_code": resp.status_code, "ok": not resp.is_error, "data": data}


def exchange_authorization_code(code: str) -> dict[str, Any]:
    app_id = (settings.META_APP_ID or "").strip()
    app_secret = (settings.META_APP_SECRET or "").strip()
    if not app_id or not app_secret:
        raise HTTPException(status_code=400, detail="Meta Embedded Signup is not configured")
    token_code = (code or "").strip()
    if not token_code or len(token_code) > 4096:
        raise HTTPException(status_code=400, detail="Authorization code is required")
    url = f"https://graph.facebook.com/{_graph_version()}/oauth/access_token"
    try:
        with httpx.Client(timeout=_timeout()) as client:
            resp = client.get(
                url,
                params={
                    "client_id": app_id,
                    "client_secret": app_secret,
                    "code": token_code,
                },
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=502, detail="Meta authorization timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail="Meta authorization failed") from exc
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.is_error or not isinstance(data, dict) or not str(data.get("access_token") or "").strip():
        logger.warning("meta_onboarding code exchange failed status=%s", resp.status_code)
        raise HTTPException(status_code=400, detail="Could not complete Meta authorization")
    expires_in = data.get("expires_in")
    expires_at = None
    try:
        seconds = int(expires_in)
        if seconds > 0:
            expires_at = utcnow() + timedelta(seconds=seconds)
    except (TypeError, ValueError):
        expires_at = None
    return {
        "access_token": str(data.get("access_token")).strip(),
        "expires_at": expires_at,
        "token_type": str(data.get("token_type") or "").strip() or None,
    }


def verify_waba_and_phone(*, access_token: str, waba_id: str, phone_number_id: str) -> dict[str, Any]:
    waba = validate_graph_id(waba_id, field="waba_id")
    pnid = validate_graph_id(phone_number_id, field="phone_number_id")
    if not waba or not pnid:
        raise HTTPException(status_code=400, detail="WABA ID and phone number ID are required")
    try:
        waba_doc = _graph_get(waba, token=access_token, params={"fields": "id,name"})
    except MetaOnboardingError as exc:
        raise HTTPException(status_code=400, detail="Could not verify WhatsApp Business account") from exc
    if str(waba_doc.get("id") or "").strip() != waba:
        raise HTTPException(status_code=400, detail="WhatsApp Business account could not be verified")
    try:
        phones = _graph_get(
            f"{waba}/phone_numbers",
            token=access_token,
            params={"fields": "id,display_phone_number,verified_name"},
        )
    except MetaOnboardingError as exc:
        raise HTTPException(status_code=400, detail="Could not verify WhatsApp phone number") from exc
    rows = phones.get("data") if isinstance(phones.get("data"), list) else []
    match = None
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") or "").strip() == pnid:
            match = row
            break
    if not match:
        raise HTTPException(
            status_code=400,
            detail="Phone number is not available on the verified WhatsApp Business account",
        )
    display = str(match.get("display_phone_number") or "").strip() or None
    if display:
        from app.services.meta_whatsapp_service import normalize_meta_phone

        display = normalize_meta_phone(display) or display
    return {
        "waba_id": waba,
        "phone_number_id": pnid,
        "display_phone_number": display,
        "waba_name": str(waba_doc.get("name") or "").strip() or None,
        "verified_name": str(match.get("verified_name") or "").strip() or None,
    }


def subscribe_waba(*, access_token: str, waba_id: str) -> bool:
    result = _graph_post(f"{waba_id}/subscribed_apps", token=access_token)
    data = result.get("data") or {}
    if result.get("ok") and (data.get("success") is True or data.get("success") == "true"):
        return True
    if result.get("ok") and not data.get("error"):
        return True
    logger.warning("meta_onboarding subscribed_apps failed status=%s", result.get("status_code"))
    return False


def register_phone_number(*, access_token: str, phone_number_id: str) -> tuple[bool, Optional[str]]:
    """Cloud API register. Embedded Signup still requires this for new numbers.

    PIN is not invented. Already-registered numbers succeed. PIN-required failures
    return (False, warning) so the caller can set an honest error status.
    """
    result = _graph_post(
        f"{phone_number_id}/register",
        token=access_token,
        json_body={"messaging_product": "whatsapp"},
    )
    data = result.get("data") or {}
    if result.get("ok") and (data.get("success") is True or not data.get("error")):
        return True, None
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = str(err.get("code") or "").strip()
    if code in _ALREADY_REGISTERED:
        return True, None
    logger.warning("meta_onboarding register phone failed status=%s", result.get("status_code"))
    return False, "WhatsApp phone number registration is incomplete"


async def _assert_pnid_available(db, *, user_id: str, pnid: str) -> None:
    clash = await db.users.find_one(
        {
            "meta_phone_number_id": pnid,
            "_id": {"$ne": ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id},
        }
    )
    if clash:
        raise HTTPException(status_code=409, detail="That WhatsApp number is already linked")


async def complete_onboarding(
    db,
    *,
    user: dict,
    state: str,
    code: str,
    waba_id: str,
    phone_number_id: str,
    display_phone_number: Optional[str] = None,
    business_id: Optional[str] = None,
    cred_db=None,
) -> dict[str, Any]:
    uid = str(user.get("_id") or "")
    session = assert_session_for_user(state=state, user_id=uid)
    _acquire_inflight(state)
    previous_status = str(user.get("meta_connection_status") or "").strip().lower()
    try:
        exchanged = exchange_authorization_code(code)
        token = exchanged["access_token"]
        verified = verify_waba_and_phone(
            access_token=token,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
        )
        pnid = verified["phone_number_id"]
        waba = verified["waba_id"]
        await _assert_pnid_available(db, user_id=uid, pnid=pnid)

        # Mongo user + credential writes are not a single transaction. Snapshot
        # the previous ciphertext so a unique-index race can restore it.
        previous_cred = snapshot_credentials_row(user_id=uid, db=cred_db)
        upsert_encrypted_access_token(
            user_id=uid,
            access_token=token,
            phone_number_id=pnid,
            expires_at=exchanged.get("expires_at"),
            db=cred_db,
        )
        subscribed = subscribe_waba(access_token=token, waba_id=waba)
        registered, register_warning = register_phone_number(
            access_token=token, phone_number_id=pnid
        )
        now = utcnow()
        display = verified.get("display_phone_number") or display_phone_number
        status = "connected"
        warnings: list[str] = []
        if not subscribed:
            status = "error"
            warnings.append("WhatsApp webhook subscription did not complete")
        if not registered:
            status = "error"
            if register_warning:
                warnings.append(register_warning)

        set_doc: dict[str, Any] = {
            "meta_connection_status": status,
            "meta_phone_number_id": pnid,
            "meta_waba_id": waba,
            "meta_display_phone_number": display,
            "meta_onboarding_source": "embedded_signup",
            "meta_connected_at": now if status == "connected" else user.get("meta_connected_at"),
            "meta_disconnected_at": None,
            "meta_token_expires_at": exchanged.get("expires_at"),
            "updated_at": now,
        }
        biz = validate_graph_id(business_id, field="business_id") if business_id else None
        if biz:
            set_doc["meta_business_id"] = biz
        try:
            await db.users.update_one({"_id": ObjectId(uid)}, {"$set": set_doc})
        except DuplicateKeyError as exc:
            restore_credentials_snapshot(user_id=uid, snapshot=previous_cred, db=cred_db)
            raise HTTPException(
                status_code=409,
                detail="That WhatsApp number is already linked",
            ) from exc

        _mark_consumed(state, session)
        action = "settings.meta_reconnected" if previous_status in ("connected", "legacy_poc", "error") else "settings.meta_connected"
        return {
            "ok": status == "connected",
            "status": status,
            "warnings": warnings,
            "audit_action": action,
            "masked": {
                "phone_number_id": _mask_id(pnid),
                "waba_id": _mask_id(waba),
            },
            "reconnected": previous_status in ("connected", "legacy_poc", "error"),
        }
    except HTTPException:
        _release_inflight(state)
        raise
    except MetaOnboardingError as exc:
        _release_inflight(state)
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        _release_inflight(state)
        logger.exception("meta_onboarding complete failed")
        raise HTTPException(status_code=502, detail="Could not complete Meta onboarding") from exc


async def disconnect_meta(db, *, user: dict, cred_db=None) -> dict[str, Any]:
    uid = str(user.get("_id") or "")
    if not uid:
        raise HTTPException(status_code=400, detail="Invalid account")
    pnid = str(user.get("meta_phone_number_id") or "").strip() or None
    now = utcnow()
    delete_credentials_for_user(user_id=uid, db=cred_db)
    set_doc: dict[str, Any] = {
        "meta_connection_status": "disconnected",
        "meta_disconnected_at": now,
        "meta_connected_at": user.get("meta_connected_at"),
        "updated_at": now,
    }
    # Release unique routing PNID so another tenant can bind. Keep last PNID
    # for historical Meta delivery/read callbacks (2I-A status scoping).
    if pnid:
        set_doc["meta_last_phone_number_id"] = pnid
        set_doc["meta_phone_number_id"] = None
    waba = str(user.get("meta_waba_id") or "").strip() or None
    if waba:
        set_doc["meta_last_waba_id"] = waba
    await db.users.update_one({"_id": ObjectId(uid)}, {"$set": set_doc})
    return {
        "ok": True,
        "masked": {"phone_number_id": _mask_id(pnid)},
    }
