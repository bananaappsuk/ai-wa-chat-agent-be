"""Stripe SDK wrappers for Checkout, Customer Portal, and invoices."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import timedelta
from typing import Any, Optional

import stripe
from bson import ObjectId

from app.billing.plans import ACTIVE_SUBSCRIPTION_STATUSES, PAID_PLAN_KEYS, normalize_plan_key
from app.billing.stripe_config import get_stripe_runtime, mask_secret
from app.config import settings
from app.models.common import utcnow

logger = logging.getLogger(__name__)


class StripeNotConfiguredError(RuntimeError):
    pass


def _require_secret() -> str:
    rt = get_stripe_runtime()
    key = (rt.secret_key or "").strip()
    if not key:
        raise StripeNotConfiguredError("Stripe is not configured (secret key missing for STRIPE_MODE)")
    stripe.api_key = key
    return key


def ensure_stripe() -> None:
    _require_secret()


def _lock_stale(lock: Any) -> bool:
    if not isinstance(lock, dict):
        return True
    at = lock.get("at")
    if at is None:
        return True
    try:
        return (utcnow() - at) > timedelta(seconds=max(5, int(settings.STRIPE_CUSTOMER_LOCK_TTL_SECONDS or 60)))
    except Exception:
        return True


async def get_or_create_customer_async(db, user: dict) -> str:
    """Race-safe Stripe Customer create/reuse (Mongo lock + Stripe idempotency key)."""
    ensure_stripe()
    user_id = str(user["_id"])
    oid = ObjectId(user_id)

    async def _read_cid() -> Optional[str]:
        doc = await db.users.find_one({"_id": oid}, {"stripe_customer_id": 1, "stripe_customer_create_lock": 1})
        if not doc:
            return None
        cid = (doc.get("stripe_customer_id") or "").strip()
        return cid or None

    async def _customer_exists_in_active_mode(customer_id: str) -> bool:
        """Return False when a stored cus_* is missing (e.g. test ID used after switching to live)."""
        try:
            stripe.Customer.retrieve(customer_id)
            return True
        except stripe.error.InvalidRequestError as exc:
            msg = str(exc).lower()
            if "no such customer" in msg:
                logger.warning(
                    "Stale Stripe customer id for user=%s customer=%s — will recreate in %s mode",
                    user_id,
                    mask_secret(customer_id, keep=6),
                    get_stripe_runtime().mode,
                )
                await db.users.update_one(
                    {"_id": oid},
                    {
                        "$unset": {"stripe_customer_id": ""},
                        "$set": {"updated_at": utcnow()},
                    },
                )
                user.pop("stripe_customer_id", None)
                return False
            raise

    existing = (user.get("stripe_customer_id") or "").strip() or await _read_cid()
    if existing:
        if await _customer_exists_in_active_mode(existing):
            user["stripe_customer_id"] = existing
            return existing
        # fall through and create a customer for the active Stripe mode

    lock_token = str(uuid.uuid4())
    now = utcnow()

    # Claim lock when no customer id (or stale lock)
    for _attempt in range(3):
        doc = await db.users.find_one({"_id": oid}, {"stripe_customer_id": 1, "stripe_customer_create_lock": 1})
        if not doc:
            raise RuntimeError("User not found")
        cid = (doc.get("stripe_customer_id") or "").strip()
        if cid:
            if await _customer_exists_in_active_mode(cid):
                user["stripe_customer_id"] = cid
                return cid
            # stale cleared — continue claiming lock to create
        lock = doc.get("stripe_customer_create_lock")
        if lock and not _lock_stale(lock) and lock.get("token") != lock_token:
            await asyncio.sleep(0.05)
            continue

        res = await db.users.find_one_and_update(
            {
                "_id": oid,
                "$or": [
                    {"stripe_customer_id": {"$in": [None, ""]}},
                    {"stripe_customer_id": {"$exists": False}},
                ],
            },
            {"$set": {"stripe_customer_create_lock": {"token": lock_token, "at": now}}},
            return_document=True,
        )
        if res is not None:
            break
    else:
        cid = await _read_cid()
        if cid and await _customer_exists_in_active_mode(cid):
            user["stripe_customer_id"] = cid
            return cid
        if not cid:
            raise RuntimeError("Timed out waiting for Stripe customer creation lock")
        # Stale id cleared above; retry once by falling through is not possible here
        raise RuntimeError("Timed out waiting for Stripe customer creation lock")

    email = (user.get("email") or "").strip() or None
    name = (user.get("full_name") or user.get("display_name") or "").strip() or None
    rt = get_stripe_runtime()

    try:
        customer = stripe.Customer.create(
            email=email,
            name=name,
            metadata={
                "user_id": user_id,
                "app_environment": settings.APP_ENV,
                "stripe_mode": rt.mode,
            },
            idempotency_key=f"wa-customer-{rt.mode}-{user_id}",
        )
        cid = customer["id"]
        await db.users.update_one(
            {"_id": oid},
            {
                "$set": {"stripe_customer_id": cid, "updated_at": utcnow()},
                "$unset": {"stripe_customer_create_lock": ""},
            },
        )
        user["stripe_customer_id"] = cid
        logger.info("Stripe customer ready user=%s customer=%s", user_id, mask_secret(cid, keep=6))
        return cid
    except Exception:
        await db.users.update_one(
            {"_id": oid, "stripe_customer_create_lock.token": lock_token},
            {"$unset": {"stripe_customer_create_lock": ""}},
        )
        cid = await _read_cid()
        if cid:
            user["stripe_customer_id"] = cid
            return cid
        raise


def create_checkout_session(
    user: dict,
    plan_key: str,
    *,
    customer_id: str,
    request_nonce: Optional[str] = None,
) -> dict[str, Any]:
    ensure_stripe()
    rt = get_stripe_runtime()
    key = normalize_plan_key(plan_key)
    if key not in PAID_PLAN_KEYS:
        raise ValueError("Plan is not available for Checkout")

    price_id = rt.price_for_plan(key)
    if not price_id:
        raise ValueError(f"Stripe price is not configured for plan '{key}' in {rt.mode} mode")
    rt.assert_price_matches_mode(price_id)

    user_id = str(user.get("_id") or user.get("id"))
    trial_days = max(0, int(settings.STRIPE_TRIAL_DAYS or 0))
    nonce = (request_nonce or str(uuid.uuid4())).replace("-", "")[:16]
    bucket = int(time.time() // 300)
    idem = f"wa-checkout-{user_id}-{key}-{bucket}-{nonce}"

    params: dict[str, Any] = {
        "mode": "subscription",
        "customer": customer_id,
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": settings.STRIPE_SUCCESS_URL,
        "cancel_url": settings.STRIPE_CANCEL_URL,
        "client_reference_id": user_id,
        "allow_promotion_codes": True,
        "metadata": {
            "user_id": user_id,
            "app_plan": key,
            "app_environment": settings.APP_ENV,
            "stripe_mode": rt.mode,
        },
        "subscription_data": {
            "metadata": {
                "user_id": user_id,
                "app_plan": key,
                "app_environment": settings.APP_ENV,
                "stripe_mode": rt.mode,
            },
        },
    }
    if trial_days > 0:
        params["subscription_data"]["trial_period_days"] = trial_days

    session = stripe.checkout.Session.create(**params, idempotency_key=idem)
    return {"id": session["id"], "url": session["url"]}


def create_portal_session(*, customer_id: str) -> dict[str, Any]:
    ensure_stripe()
    if not customer_id:
        raise ValueError("No Stripe customer on file")
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=settings.STRIPE_PORTAL_RETURN_URL,
        )
    except stripe.error.InvalidRequestError as exc:
        msg = str(exc)
        if "portal" in msg.lower() or "configuration" in msg.lower():
            raise RuntimeError(
                "Stripe Customer Portal is not configured. "
                "Enable it in Stripe Dashboard → Settings → Billing → Customer portal."
            ) from exc
        raise
    return {"url": session["url"]}


def list_invoices(*, customer_id: str, limit: int = 24, starting_after: Optional[str] = None) -> dict[str, Any]:
    ensure_stripe()
    if not customer_id:
        return {"items": [], "has_more": False}
    kwargs: dict[str, Any] = {"customer": customer_id, "limit": min(100, max(1, limit))}
    if starting_after:
        kwargs["starting_after"] = starting_after
    result = stripe.Invoice.list(**kwargs)
    out: list[dict[str, Any]] = []
    for inv in result.data:
        paid_at = None
        try:
            paid_at = (inv.get("status_transitions") or {}).get("paid_at")
        except Exception:
            paid_at = None
        out.append(
            {
                "id": inv.get("id"),
                "number": inv.get("number"),
                "status": (inv.get("status") or "").lower() or None,
                "currency": inv.get("currency"),
                "amount_due": inv.get("amount_due"),
                "amount_paid": inv.get("amount_paid"),
                "created": inv.get("created"),
                "paid_at": paid_at,
                "hosted_invoice_url": inv.get("hosted_invoice_url"),
                "invoice_pdf": inv.get("invoice_pdf"),
                "period_start": inv.get("period_start"),
                "period_end": inv.get("period_end"),
            }
        )
    return {"items": out, "has_more": bool(result.has_more)}


def construct_event(payload: bytes, sig_header: str) -> stripe.Event:
    """Verify Stripe signature using the raw request body (never pre-parsed JSON)."""
    rt = get_stripe_runtime()
    secret = (rt.webhook_secret or "").strip()
    if not secret:
        raise StripeNotConfiguredError("Stripe webhook secret is not configured for STRIPE_MODE")
    ensure_stripe()
    logger.info(
        "construct_event mode=%s secret_source=%s payload_bytes=%s",
        rt.mode,
        rt.webhook_secret_source,
        len(payload or b""),
    )
    return stripe.Webhook.construct_event(payload, sig_header, secret)


def user_has_active_subscription(user: dict) -> bool:
    status = (user.get("subscription_status") or "none").strip().lower()
    if status in ("incomplete", "incomplete_expired", "manual_override"):
        return False
    return status in ACTIVE_SUBSCRIPTION_STATUSES and bool(user.get("stripe_subscription_id"))


def subscription_price_id(subscription: Any) -> Optional[str]:
    try:
        items = subscription.get("items", {}).get("data") or []
        if not items:
            return None
        price = items[0].get("price") or {}
        if isinstance(price, str):
            return price
        return price.get("id")
    except Exception:
        return None


def subscription_product_id(subscription: Any) -> Optional[str]:
    try:
        items = subscription.get("items", {}).get("data") or []
        if not items:
            return None
        price = items[0].get("price") or {}
        if isinstance(price, str):
            return None
        product = price.get("product")
        if isinstance(product, dict):
            return product.get("id")
        return product
    except Exception:
        return None
