"""Billing API: plans, Checkout, Portal, subscription, invoices, Stripe webhooks."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.billing.plans import (
    ACTIVE_SUBSCRIPTION_STATUSES,
    PAID_PLAN_KEYS,
    PLAN_CATALOG,
    entitlements_for_plan,
    normalize_plan_key,
    plan_from_price_id,
    public_plans,
)
from app.billing.stripe_config import get_stripe_runtime, mask_secret
from app.config import settings
from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.common import utcnow
from app.services import stripe_service
from app.services.stripe_service import StripeNotConfiguredError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

WEBHOOK_CLAIM_TTL = lambda: timedelta(seconds=max(30, int(settings.STRIPE_WEBHOOK_CLAIM_TTL_SECONDS or 300)))


class CheckoutBody(BaseModel):
    plan: str = Field(min_length=1, max_length=32)


def _dt_from_ts(ts: Any) -> Optional[datetime]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc)
    except Exception:
        return None


def _iso(dt: Any) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    return str(dt)


def _trial_days_remaining(trial_end: Any) -> Optional[int]:
    if trial_end is None:
        return None
    try:
        if isinstance(trial_end, str):
            end = datetime.fromisoformat(trial_end.replace("Z", "+00:00"))
        elif isinstance(trial_end, datetime):
            end = trial_end if trial_end.tzinfo else trial_end.replace(tzinfo=timezone.utc)
        else:
            return None
        delta = end - utcnow()
        return max(0, int(delta.total_seconds() // 86400))
    except Exception:
        return None


@router.get("/plans")
async def list_plans() -> dict[str, Any]:
    rt = get_stripe_runtime()
    return {
        "plans": public_plans(include_free=False),
        "trial_days": int(settings.STRIPE_TRIAL_DAYS or 0),
        "currency": "gbp",
        "billing_cycle": "monthly",
        "stripe_mode": rt.mode,
        "publishable_key": rt.publishable_key or None,
        "contact_sales_url": settings.BILLING_CONTACT_SALES_URL,
        "disclaimer": (
            "Twilio/Meta WhatsApp conversation charges are billed separately from your subscription."
        ),
        "addons_note": (
            "Optional add-ons (extra team members, WhatsApp numbers, AI conversations, "
            "custom agent setup, premium onboarding) coming soon."
        ),
    }


@router.get("/subscription")
async def get_subscription(user: dict = Depends(current_user)) -> dict[str, Any]:
    plan = normalize_plan_key(user.get("plan"))
    status = (user.get("subscription_status") or "none").strip().lower() or "none"
    catalog = PLAN_CATALOG.get(plan, PLAN_CATALOG["free"])
    rt = get_stripe_runtime()
    trial_end = user.get("trial_ends_at") or user.get("trial_end")
    return {
        "plan": plan,
        "plan_name": catalog["name"],
        "price_display": catalog.get("price_display"),
        "billing_cycle": "Monthly",
        "subscription_status": status,
        "stripe_mode": rt.mode,
        "stripe_customer_id": user.get("stripe_customer_id"),
        "stripe_subscription_id": user.get("stripe_subscription_id"),
        "stripe_price_id": user.get("stripe_price_id"),
        "stripe_product_id": user.get("stripe_product_id"),
        "trial_start": _iso(user.get("trial_start")),
        "trial_ends_at": _iso(trial_end),
        "trial_days_remaining": _trial_days_remaining(trial_end),
        "current_period_start": _iso(user.get("current_period_start")),
        "current_period_end": _iso(user.get("current_period_end")),
        "cancel_at_period_end": bool(user.get("cancel_at_period_end")),
        "cancelled_at": _iso(user.get("cancelled_at")),
        "latest_invoice_id": user.get("latest_invoice_id"),
        "last_payment_status": user.get("last_payment_status"),
        "last_payment_at": _iso(user.get("last_payment_at")),
        "entitlements": entitlements_for_plan(plan),
        "has_active_subscription": status in ACTIVE_SUBSCRIPTION_STATUSES
        and bool(user.get("stripe_subscription_id")),
        "can_checkout": plan == "free"
        or status in ("none", "canceled", "incomplete", "incomplete_expired")
        or not user.get("stripe_subscription_id"),
        "can_manage": bool(user.get("stripe_customer_id")),
        "use_portal_for_changes": bool(
            user.get("stripe_subscription_id")
            and status in ACTIVE_SUBSCRIPTION_STATUSES | {"unpaid", "past_due"}
        ),
    }


@router.get("/invoices")
async def get_invoices(
    user: dict = Depends(current_user),
    limit: int = Query(default=24, ge=1, le=100),
    starting_after: Optional[str] = Query(default=None, max_length=128),
) -> dict[str, Any]:
    customer_id = (user.get("stripe_customer_id") or "").strip()
    if not customer_id:
        return {"items": [], "has_more": False}
    try:
        return stripe_service.list_invoices(
            customer_id=customer_id, limit=limit, starting_after=starting_after
        )
    except StripeNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to list invoices")
        raise HTTPException(status_code=502, detail="Unable to load invoices from Stripe") from exc


@router.post("/checkout")
async def create_checkout(body: CheckoutBody, user: dict = Depends(current_user)) -> dict[str, Any]:
    plan = normalize_plan_key(body.plan)
    if plan == "enterprise":
        raise HTTPException(status_code=400, detail="Enterprise plans require Contact Sales")
    if plan == "free":
        raise HTTPException(status_code=400, detail="Free plan does not require Checkout")
    if plan not in PAID_PLAN_KEYS:
        raise HTTPException(status_code=400, detail="Invalid plan for checkout")

    status = (user.get("subscription_status") or "none").strip().lower()
    if stripe_service.user_has_active_subscription(user) or (
        user.get("stripe_subscription_id") and status in ("past_due", "unpaid")
    ):
        current = normalize_plan_key(user.get("plan"))
        if current == plan and status in ACTIVE_SUBSCRIPTION_STATUSES:
            raise HTTPException(status_code=400, detail="You are already on this plan")
        raise HTTPException(
            status_code=409,
            detail={
                "code": "use_portal",
                "message": "You already have a Stripe subscription. Use Manage Subscription to change plans.",
            },
        )

    db = get_db()
    try:
        customer_id = await stripe_service.get_or_create_customer_async(db, user)
        # Ensure customer on user matches
        if user.get("stripe_customer_id") and user["stripe_customer_id"] != customer_id:
            raise HTTPException(status_code=409, detail="Stripe customer mismatch")
        session = stripe_service.create_checkout_session(user, plan, customer_id=customer_id)
    except StripeNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Checkout session failed")
        # Prefer Stripe's message when available (still safe for the UI)
        detail = "Unable to start Stripe Checkout"
        stripe_msg = getattr(exc, "user_message", None) or str(exc)
        if stripe_msg and "No such customer" in stripe_msg:
            detail = (
                "Stripe customer is invalid for live mode. "
                "Retry checkout — a new live customer will be created."
            )
        elif stripe_msg and len(stripe_msg) < 240:
            detail = f"Unable to start Stripe Checkout: {stripe_msg}"
        raise HTTPException(status_code=502, detail=detail) from exc

    if not session.get("url"):
        raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
    return {"url": session["url"]}


@router.post("/portal")
async def create_portal(user: dict = Depends(current_user)) -> dict[str, str]:
    customer_id = (user.get("stripe_customer_id") or "").strip()
    if not customer_id:
        raise HTTPException(
            status_code=400,
            detail="No billing customer yet. Start a subscription first.",
        )
    # Ownership: customer id must be on the authenticated user document only
    db = get_db()
    owner = await db.users.find_one(
        {"_id": user["_id"], "stripe_customer_id": customer_id},
        {"_id": 1},
    )
    if not owner:
        raise HTTPException(status_code=403, detail="Stripe customer does not belong to this account")

    try:
        session = stripe_service.create_portal_session(customer_id=customer_id)
    except StripeNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Portal session failed")
        raise HTTPException(status_code=502, detail="Unable to open billing portal") from exc
    return {"url": session["url"]}


# --- Webhook claim / idempotency -------------------------------------------------

def _as_aware_utc(dt: Any) -> Optional[datetime]:
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _claim_webhook_event(db, event_id: str, event_type: str) -> str:
    """
    Atomically claim an event. Returns: 'claimed' | 'processed' | 'busy'.
    """
    now = utcnow()
    stale_before = now - WEBHOOK_CLAIM_TTL()

    existing = await db.stripe_webhook_events.find_one({"_id": event_id})
    if existing:
        status = existing.get("status")
        if status == "processed":
            return "processed"
        if status == "processing":
            started = _as_aware_utc(existing.get("processing_started_at"))
            if started and started > stale_before:
                return "busy"
            # stale — fall through to reclaim
        # failed or stale processing — reclaim below

    # Try insert
    try:
        await db.stripe_webhook_events.insert_one(
            {
                "_id": event_id,
                "event_type": event_type,
                "status": "processing",
                "attempt_count": 1,
                "received_at": now,
                "processing_started_at": now,
            }
        )
        return "claimed"
    except Exception:
        # Duplicate key or race — try reclaim failed/stale
        res = await db.stripe_webhook_events.find_one_and_update(
            {
                "_id": event_id,
                "$or": [
                    {"status": "failed"},
                    {"status": "processing", "processing_started_at": {"$lte": stale_before}},
                    {"status": {"$exists": False}},
                ],
            },
            {
                "$set": {
                    "status": "processing",
                    "event_type": event_type,
                    "processing_started_at": now,
                    "last_error": None,
                },
                "$inc": {"attempt_count": 1},
                "$setOnInsert": {"received_at": now},
            },
            return_document=True,
        )
        if res and res.get("status") == "processing":
            return "claimed"
        again = await db.stripe_webhook_events.find_one({"_id": event_id})
        if again and again.get("status") == "processed":
            return "processed"
        return "busy"


async def _mark_webhook_processed(db, event_id: str) -> None:
    await db.stripe_webhook_events.update_one(
        {"_id": event_id},
        {"$set": {"status": "processed", "processed_at": utcnow(), "last_error": None}},
    )


async def _mark_webhook_failed(db, event_id: str, error: str) -> None:
    await db.stripe_webhook_events.update_one(
        {"_id": event_id},
        {
            "$set": {
                "status": "failed",
                "failed_at": utcnow(),
                "last_error": (error or "")[:500],
            }
        },
    )


class WebhookUserConflict(Exception):
    pass


async def _resolve_user_id(
    db,
    *,
    metadata: Optional[dict] = None,
    customer_id: Optional[str] = None,
    subscription_id: Optional[str] = None,
) -> Optional[str]:
    meta = metadata or {}
    uid = (meta.get("user_id") or "").strip()
    if uid and ObjectId.is_valid(uid):
        user = await db.users.find_one({"_id": ObjectId(uid)})
        if not user:
            raise WebhookUserConflict(f"metadata user_id not found: {uid}")
        stored_cus = (user.get("stripe_customer_id") or "").strip()
        if customer_id and stored_cus and stored_cus != customer_id:
            raise WebhookUserConflict("stripe_customer_id mismatch for metadata user_id")
        stored_sub = (user.get("stripe_subscription_id") or "").strip()
        if subscription_id and stored_sub and stored_sub != subscription_id and stored_cus:
            # allow subscription id change on upgrade; only conflict if customer also mismatches
            pass
        return uid

    if customer_id:
        # Prefer customer metadata via Stripe is not available here; DB lookup
        user = await db.users.find_one({"stripe_customer_id": customer_id})
        if user:
            return str(user["_id"])

    if subscription_id:
        user = await db.users.find_one({"stripe_subscription_id": subscription_id})
        if user:
            return str(user["_id"])

    return None


def _subscription_fields(subscription: dict, *, deleted: bool = False) -> dict[str, Any]:
    now = utcnow()
    if deleted:
        return {
            "plan": "free",
            "subscription_status": "canceled",
            "cancel_at_period_end": False,
            "cancelled_at": _dt_from_ts(subscription.get("canceled_at")) or now,
            "subscription_updated_at": now,
            "updated_at": now,
            # keep stripe ids for audit
            "stripe_subscription_id": subscription.get("id"),
        }

    price_id = stripe_service.subscription_price_id(subscription)
    product_id = stripe_service.subscription_product_id(subscription)
    plan = plan_from_price_id(price_id)
    if not plan:
        meta_plan = normalize_plan_key(
            (subscription.get("metadata") or {}).get("app_plan")
            or (subscription.get("metadata") or {}).get("plan")
        )
        if meta_plan in PAID_PLAN_KEYS:
            # Prefer price mapping; metadata alone is weaker but used only if price not configured
            if price_id:
                raise ValueError(f"Unmapped Stripe price for active mode: {mask_secret(price_id, keep=6)}")
            plan = meta_plan
        else:
            plan = "free"

    status = (subscription.get("status") or "active").strip().lower()
    fields: dict[str, Any] = {
        "plan": plan if status != "canceled" else "free",
        "subscription_status": status,
        "stripe_subscription_id": subscription.get("id"),
        "stripe_price_id": price_id,
        "stripe_product_id": product_id,
        "cancel_at_period_end": bool(subscription.get("cancel_at_period_end")),
        "current_period_start": _dt_from_ts(subscription.get("current_period_start")),
        "current_period_end": _dt_from_ts(subscription.get("current_period_end")),
        "trial_start": _dt_from_ts(subscription.get("trial_start")),
        "trial_ends_at": _dt_from_ts(subscription.get("trial_end")),
        "trial_end": _dt_from_ts(subscription.get("trial_end")),
        "subscription_updated_at": now,
        "updated_at": now,
    }
    if not fields.get("subscription_created_at") and subscription.get("created"):
        fields["subscription_created_at"] = _dt_from_ts(subscription.get("created"))

    customer_id = subscription.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    if customer_id:
        fields["stripe_customer_id"] = customer_id

    if status == "canceled":
        fields["plan"] = "free"
        fields["cancelled_at"] = _dt_from_ts(subscription.get("canceled_at")) or now

    return fields


async def _apply_subscription(db, subscription: dict, *, deleted: bool = False) -> None:
    customer_id = subscription.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    user_id = await _resolve_user_id(
        db,
        metadata=subscription.get("metadata") or {},
        customer_id=customer_id,
        subscription_id=subscription.get("id"),
    )
    if not user_id:
        logger.warning(
            "Stripe subscription event without resolvable user sub=%s",
            mask_secret(subscription.get("id"), keep=6),
        )
        return

    fields = _subscription_fields(subscription, deleted=deleted)
    # Preserve subscription_created_at if already set
    existing = await db.users.find_one({"_id": ObjectId(user_id)}, {"subscription_created_at": 1})
    if existing and existing.get("subscription_created_at") and "subscription_created_at" in fields:
        del fields["subscription_created_at"]
    elif not existing or not existing.get("subscription_created_at"):
        if not deleted and subscription.get("created"):
            fields["subscription_created_at"] = _dt_from_ts(subscription.get("created"))

    await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": fields})


async def _handle_checkout_completed(db, session: dict) -> None:
    user_id = (
        session.get("client_reference_id")
        or (session.get("metadata") or {}).get("user_id")
        or ""
    ).strip()
    customer_id = session.get("customer")
    subscription_id = session.get("subscription")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    if isinstance(subscription_id, dict):
        subscription_id = subscription_id.get("id")

    if not user_id or not ObjectId.is_valid(user_id):
        # try metadata resolution
        user_id = await _resolve_user_id(
            db,
            metadata=session.get("metadata") or {},
            customer_id=customer_id,
            subscription_id=subscription_id,
        ) or ""
    if not user_id or not ObjectId.is_valid(user_id):
        logger.warning("checkout.session.completed missing user_id")
        return

    # Verify user / customer consistency
    await _resolve_user_id(
        db,
        metadata={"user_id": user_id},
        customer_id=customer_id,
        subscription_id=subscription_id,
    )

    fields: dict[str, Any] = {"updated_at": utcnow(), "subscription_updated_at": utcnow()}
    if customer_id:
        fields["stripe_customer_id"] = customer_id
    if subscription_id:
        fields["stripe_subscription_id"] = subscription_id

    # Prefer price from line items if present
    plan = None
    try:
        items = (session.get("line_items") or {}).get("data") or []
        # line_items often not expanded — fall back to metadata app_plan after price map
    except Exception:
        items = []
    meta_plan = normalize_plan_key(
        (session.get("metadata") or {}).get("app_plan")
        or (session.get("metadata") or {}).get("plan")
    )
    if meta_plan in PAID_PLAN_KEYS:
        # Will be refined by subscription.* with authoritative price id
        fields["plan"] = meta_plan
        fields["subscription_status"] = (
            "trialing" if int(settings.STRIPE_TRIAL_DAYS or 0) > 0 else "active"
        )

    await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": fields})


async def _handle_invoice(db, invoice: dict, *, payment_status: str) -> None:
    customer_id = invoice.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    sub_id = invoice.get("subscription")
    if isinstance(sub_id, dict):
        sub_id = sub_id.get("id")

    user_id = await _resolve_user_id(
        db,
        metadata=invoice.get("metadata") or {},
        customer_id=customer_id,
        subscription_id=sub_id,
    )
    if not user_id:
        return

    fields: dict[str, Any] = {
        "latest_invoice_id": invoice.get("id"),
        "last_payment_status": payment_status,
        "subscription_updated_at": utcnow(),
        "updated_at": utcnow(),
    }
    if payment_status == "paid":
        fields["last_payment_at"] = _dt_from_ts(invoice.get("status_transitions", {}).get("paid_at")) or utcnow()
        # Do not force plan; subscription events own mapping. Soft-set active if not past_due already.
        user = await db.users.find_one({"_id": ObjectId(user_id)}, {"subscription_status": 1})
        st = (user or {}).get("subscription_status")
        if st in (None, "none", "incomplete", "trialing", "past_due"):
            fields["subscription_status"] = "active" if st != "trialing" else st
    elif payment_status == "failed":
        fields["subscription_status"] = "past_due"
    elif payment_status == "action_required":
        fields["last_payment_status"] = "action_required"
    # finalized → only latest_invoice_id

    await db.users.update_one({"_id": ObjectId(user_id)}, {"$set": fields})


async def _handle_trial_will_end(db, subscription: dict) -> None:
    customer_id = subscription.get("customer")
    if isinstance(customer_id, dict):
        customer_id = customer_id.get("id")
    user_id = await _resolve_user_id(
        db,
        metadata=subscription.get("metadata") or {},
        customer_id=customer_id,
        subscription_id=subscription.get("id"),
    )
    if not user_id:
        return
    trial_end = _dt_from_ts(subscription.get("trial_end"))
    await db.users.update_one(
        {"_id": ObjectId(user_id)},
        {
            "$set": {
                "trial_ends_at": trial_end,
                "trial_end": trial_end,
                "subscription_updated_at": utcnow(),
                "updated_at": utcnow(),
            }
        },
    )
    logger.info(
        "trial_will_end user=%s trial_end=%s (notification hook reserved)",
        user_id,
        _iso(trial_end),
    )


async def _handle_checkout_expired(db, session: dict) -> None:
    logger.info(
        "checkout.session.expired session=%s user_meta=%s",
        mask_secret(session.get("id"), keep=6),
        (session.get("metadata") or {}).get("user_id"),
    )


@router.post("/webhook")
async def stripe_webhook(request: Request) -> dict[str, bool]:
    payload = await request.body()
    sig = request.headers.get("stripe-signature") or ""
    rt = get_stripe_runtime()
    logger.info(
        "Stripe webhook received mode=%s secret_source=%s payload_bytes=%s sig_present=%s",
        rt.mode,
        rt.webhook_secret_source,
        len(payload or b""),
        bool(sig),
    )
    try:
        # Raw body + Stripe-Signature only — do not parse JSON before verify.
        event = stripe_service.construct_event(payload, sig)
    except StripeNotConfiguredError as exc:
        logger.warning("Stripe webhook not configured: %s", type(exc).__name__)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.warning(
            "Stripe webhook signature verification failed mode=%s secret_source=%s exc=%s",
            rt.mode,
            rt.webhook_secret_source,
            type(exc).__name__,
        )
        raise HTTPException(status_code=400, detail="Invalid webhook signature") from exc

    db = get_db()
    event_id = event["id"]
    event_type = event["type"]
    logger.info(
        "Stripe webhook signature verified event_id=%s type=%s",
        mask_secret(event_id, keep=8),
        event_type,
    )

    claim = await _claim_webhook_event(db, event_id, event_type)
    logger.info(
        "Stripe webhook claim result=%s event_id=%s type=%s",
        claim,
        mask_secret(event_id, keep=8),
        event_type,
    )
    if claim == "processed":
        logger.info("Stripe webhook already processed event_id=%s → 200", mask_secret(event_id, keep=8))
        return {"ok": True}
    if claim == "busy":
        logger.info("Stripe webhook busy event_id=%s → 200", mask_secret(event_id, keep=8))
        return {"ok": True}

    data_object = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            meta = data_object.get("metadata") or {}
            logger.info(
                "checkout.session.completed user_lookup meta_user_id=%s client_reference_id=%s "
                "customer=%s app_plan=%s stripe_mode=%s",
                meta.get("user_id"),
                data_object.get("client_reference_id"),
                mask_secret(data_object.get("customer"), keep=6),
                meta.get("app_plan"),
                meta.get("stripe_mode"),
            )
            await _handle_checkout_completed(db, data_object)
        elif event_type == "checkout.session.expired":
            await _handle_checkout_expired(db, data_object)
        elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
            meta = data_object.get("metadata") or {}
            logger.info(
                "subscription event=%s user_lookup meta_user_id=%s customer=%s sub=%s app_plan=%s",
                event_type,
                meta.get("user_id"),
                mask_secret(data_object.get("customer"), keep=6),
                mask_secret(data_object.get("id"), keep=6),
                meta.get("app_plan"),
            )
            await _apply_subscription(db, data_object, deleted=False)
        elif event_type == "customer.subscription.deleted":
            await _apply_subscription(db, data_object, deleted=True)
        elif event_type == "customer.subscription.trial_will_end":
            await _handle_trial_will_end(db, data_object)
        elif event_type == "invoice.paid":
            await _handle_invoice(db, data_object, payment_status="paid")
        elif event_type == "invoice.payment_failed":
            await _handle_invoice(db, data_object, payment_status="failed")
        elif event_type == "invoice.finalized":
            await _handle_invoice(db, data_object, payment_status="finalized")
        elif event_type == "invoice.payment_action_required":
            await _handle_invoice(db, data_object, payment_status="action_required")
        else:
            logger.info("Ignoring Stripe event type=%s id=%s", event_type, mask_secret(event_id, keep=6))

        await _mark_webhook_processed(db, event_id)
        logger.info(
            "Stripe webhook Mongo update ok event_id=%s type=%s → 200",
            mask_secret(event_id, keep=8),
            event_type,
        )
    except WebhookUserConflict as exc:
        await _mark_webhook_failed(db, event_id, str(exc))
        logger.error("Webhook user conflict: %s", exc)
        raise HTTPException(status_code=500, detail="Webhook user conflict") from exc
    except Exception as exc:
        await _mark_webhook_failed(db, event_id, str(exc))
        logger.exception("Error handling Stripe event type=%s", event_type)
        raise HTTPException(status_code=500, detail="Webhook handler failed") from exc

    return {"ok": True}
