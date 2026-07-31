"""Billing / Stripe unit tests (mocked Stripe — no live API)."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId


def test_normalize_plan_key_aliases():
    from app.billing.plans import normalize_plan_key

    assert normalize_plan_key("pro") == "professional"
    assert normalize_plan_key("PROFESSIONAL") == "professional"
    assert normalize_plan_key("unknown") == "free"


def test_stripe_runtime_test_mode_selects_test_keys(monkeypatch):
    from app.billing import stripe_config
    from app.config import settings

    monkeypatch.setattr(settings, "STRIPE_MODE", "test")
    monkeypatch.setattr(settings, "APP_ENV", "dev")
    monkeypatch.setattr(settings, "STRIPE_TEST_SECRET_KEY", "sk_test_abc123456789")
    monkeypatch.setattr(settings, "STRIPE_LIVE_SECRET_KEY", "sk_live_should_not_use")
    monkeypatch.setattr(settings, "STRIPE_TEST_WEBHOOK_SECRET", "whsec_test")
    monkeypatch.setattr(settings, "STRIPE_TEST_PRICE_STARTER", "price_test_starter")
    monkeypatch.setattr(settings, "STRIPE_TEST_PRICE_PROFESSIONAL", "price_test_pro")
    monkeypatch.setattr(settings, "STRIPE_TEST_PRICE_BUSINESS", "price_test_biz")
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(settings, "STRIPE_PRICE_STARTER", "")
    stripe_config.clear_stripe_runtime_cache()
    rt = stripe_config.get_stripe_runtime()
    assert rt.mode == "test"
    assert rt.secret_key.startswith("sk_test_")
    assert rt.price_starter == "price_test_starter"
    stripe_config.clear_stripe_runtime_cache()


def test_production_rejects_test_mode(monkeypatch):
    from app.billing import stripe_config
    from app.config import settings

    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "STRIPE_MODE", "test")
    monkeypatch.setattr(settings, "STRIPE_TEST_SECRET_KEY", "sk_test_abc")
    monkeypatch.setattr(settings, "STRIPE_TEST_WEBHOOK_SECRET", "whsec_x")
    stripe_config.clear_stripe_runtime_cache()
    errors = stripe_config.validate_stripe_configuration()
    assert any("STRIPE_MODE must be 'live'" in e for e in errors)
    stripe_config.clear_stripe_runtime_cache()


def test_dev_rejects_live_secret_without_override(monkeypatch):
    from app.billing import stripe_config
    from app.config import settings

    monkeypatch.setattr(settings, "APP_ENV", "dev")
    monkeypatch.setattr(settings, "STRIPE_MODE", "live")
    monkeypatch.setattr(settings, "STRIPE_ALLOW_LIVE_IN_DEV", False)
    monkeypatch.setattr(settings, "STRIPE_LIVE_SECRET_KEY", "sk_live_abc123456")
    monkeypatch.setattr(settings, "STRIPE_LIVE_WEBHOOK_SECRET", "whsec_live")
    stripe_config.clear_stripe_runtime_cache()
    errors = stripe_config.validate_stripe_configuration()
    assert any("Live Stripe secret key rejected" in e for e in errors)
    stripe_config.clear_stripe_runtime_cache()


def test_production_rejects_localhost_urls(monkeypatch):
    from app.billing import stripe_config
    from app.config import settings

    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "STRIPE_MODE", "live")
    monkeypatch.setattr(settings, "STRIPE_LIVE_SECRET_KEY", "sk_live_abc1234567890")
    monkeypatch.setattr(settings, "STRIPE_LIVE_WEBHOOK_SECRET", "whsec_live")
    monkeypatch.setattr(settings, "STRIPE_LIVE_PRICE_STARTER", "price_1")
    monkeypatch.setattr(settings, "STRIPE_LIVE_PRICE_PROFESSIONAL", "price_2")
    monkeypatch.setattr(settings, "STRIPE_LIVE_PRICE_BUSINESS", "price_3")
    monkeypatch.setattr(settings, "STRIPE_SUCCESS_URL", "http://localhost:8080/billing?checkout=success")
    monkeypatch.setattr(settings, "STRIPE_CANCEL_URL", "https://app.example.com/billing?checkout=canceled")
    monkeypatch.setattr(settings, "STRIPE_PORTAL_RETURN_URL", "https://app.example.com/billing")
    stripe_config.clear_stripe_runtime_cache()
    errors = stripe_config.validate_stripe_configuration()
    assert any("STRIPE_SUCCESS_URL" in e for e in errors)
    stripe_config.clear_stripe_runtime_cache()


def test_public_plans_marks_popular():
    from app.billing.plans import public_plans

    pro = next(p for p in public_plans() if p["key"] == "professional")
    assert pro["popular"] is True


@pytest.mark.asyncio
async def test_checkout_rejects_enterprise():
    from fastapi import HTTPException
    from app.routes import billing as billing_routes

    user = {"_id": ObjectId(), "plan": "free", "subscription_status": "none"}
    with pytest.raises(HTTPException) as exc:
        await billing_routes.create_checkout(
            billing_routes.CheckoutBody(plan="enterprise"), user=user
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_checkout_rejects_free():
    from fastapi import HTTPException
    from app.routes import billing as billing_routes

    user = {"_id": ObjectId(), "plan": "free", "subscription_status": "none"}
    with pytest.raises(HTTPException) as exc:
        await billing_routes.create_checkout(billing_routes.CheckoutBody(plan="free"), user=user)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_checkout_active_subscriber_gets_portal_hint():
    from fastapi import HTTPException
    from app.routes import billing as billing_routes

    user = {
        "_id": ObjectId(),
        "plan": "starter",
        "subscription_status": "active",
        "stripe_subscription_id": "sub_123",
        "stripe_customer_id": "cus_1",
    }
    with pytest.raises(HTTPException) as exc:
        await billing_routes.create_checkout(
            billing_routes.CheckoutBody(plan="professional"), user=user
        )
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_webhook_claim_first_duplicate_processed_once():
    from app.routes import billing as billing_routes

    db = MagicMock()
    db.stripe_webhook_events.find_one = AsyncMock(
        return_value={"_id": "evt_1", "status": "processed"}
    )
    event = {"id": "evt_1", "type": "invoice.paid", "data": {"object": {}}}

    with (
        patch.object(billing_routes.stripe_service, "construct_event", return_value=event),
        patch.object(billing_routes, "get_db", return_value=db),
        patch.object(billing_routes, "_handle_invoice", new_callable=AsyncMock) as handler,
    ):
        req = MagicMock()
        req.body = AsyncMock(return_value=b"{}")
        req.headers = {"stripe-signature": "t=1,v1=abc"}
        result = await billing_routes.stripe_webhook(req)
        assert result == {"ok": True}
        handler.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_invalid_signature_returns_400():
    from fastapi import HTTPException
    from app.routes import billing as billing_routes

    with patch.object(
        billing_routes.stripe_service,
        "construct_event",
        side_effect=ValueError("bad sig"),
    ):
        req = MagicMock()
        req.body = AsyncMock(return_value=b"{}")
        req.headers = {"stripe-signature": "bad"}
        with pytest.raises(HTTPException) as exc:
            await billing_routes.stripe_webhook(req)
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_apply_subscription_maps_price_and_period(monkeypatch):
    from app.billing import stripe_config
    from app.config import settings
    from app.routes import billing as billing_routes

    monkeypatch.setattr(settings, "STRIPE_MODE", "test")
    monkeypatch.setattr(settings, "STRIPE_TEST_PRICE_PROFESSIONAL", "price_pro_test")
    monkeypatch.setattr(settings, "STRIPE_TEST_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_PRICE_PROFESSIONAL", "")
    stripe_config.clear_stripe_runtime_cache()

    user_id = ObjectId()
    db = MagicMock()
    db.users.find_one = AsyncMock(
        side_effect=[
            {"_id": user_id, "stripe_customer_id": "cus_1"},  # resolve
            {"_id": user_id},  # existing created_at check
        ]
    )
    db.users.update_one = AsyncMock()

    subscription = {
        "id": "sub_1",
        "customer": "cus_1",
        "status": "trialing",
        "cancel_at_period_end": False,
        "current_period_start": 1_700_000_000,
        "current_period_end": 1_900_000_000,
        "trial_start": 1_699_000_000,
        "trial_end": 1_800_000_000,
        "created": 1_699_000_000,
        "metadata": {"user_id": str(user_id), "app_plan": "professional"},
        "items": {"data": [{"price": {"id": "price_pro_test", "product": "prod_1"}}]},
    }

    await billing_routes._apply_subscription(db, subscription, deleted=False)
    fields = db.users.update_one.call_args[0][1]["$set"]
    assert fields["plan"] == "professional"
    assert fields["stripe_price_id"] == "price_pro_test"
    assert fields["stripe_product_id"] == "prod_1"
    assert fields["subscription_status"] == "trialing"
    assert isinstance(fields["current_period_start"], datetime)
    stripe_config.clear_stripe_runtime_cache()


@pytest.mark.asyncio
async def test_apply_subscription_deleted_sets_cancelled():
    from app.routes import billing as billing_routes

    user_id = ObjectId()
    db = MagicMock()
    db.users.find_one = AsyncMock(
        side_effect=[
            {"_id": user_id, "stripe_customer_id": "cus_1"},
            {"_id": user_id},
        ]
    )
    db.users.update_one = AsyncMock()

    subscription = {
        "id": "sub_1",
        "customer": "cus_1",
        "canceled_at": 1_800_000_000,
        "metadata": {"user_id": str(user_id)},
    }
    await billing_routes._apply_subscription(db, subscription, deleted=True)
    fields = db.users.update_one.call_args[0][1]["$set"]
    assert fields["plan"] == "free"
    assert fields["subscription_status"] == "canceled"
    assert fields.get("cancelled_at") is not None


@pytest.mark.asyncio
async def test_webhook_user_conflict_on_customer_mismatch():
    from app.routes import billing as billing_routes

    db = MagicMock()
    uid = ObjectId()
    db.users.find_one = AsyncMock(
        return_value={"_id": uid, "stripe_customer_id": "cus_other"}
    )
    with pytest.raises(billing_routes.WebhookUserConflict):
        await billing_routes._resolve_user_id(
            db, metadata={"user_id": str(uid)}, customer_id="cus_expected"
        )


@pytest.mark.asyncio
async def test_customer_create_reuses_existing(monkeypatch):
    from app.services import stripe_service

    user = {"_id": ObjectId(), "stripe_customer_id": "cus_existing", "email": "a@b.com"}
    db = MagicMock()
    with patch.object(stripe_service, "ensure_stripe"):
        cid = await stripe_service.get_or_create_customer_async(db, user)
    assert cid == "cus_existing"
    db.users.update_one.assert_not_called()


@pytest.mark.asyncio
async def test_portal_requires_ownership():
    from fastapi import HTTPException
    from app.routes import billing as billing_routes

    uid = ObjectId()
    user = {"_id": uid, "stripe_customer_id": "cus_1"}
    db = MagicMock()
    db.users.find_one = AsyncMock(return_value=None)
    with patch.object(billing_routes, "get_db", return_value=db):
        with pytest.raises(HTTPException) as exc:
            await billing_routes.create_portal(user=user)
    assert exc.value.status_code == 403


def test_mask_secret_never_prints_full_key():
    from app.billing.stripe_config import mask_secret

    masked = mask_secret("sk_test_abcdefghijklmnopqrstuvwxyz")
    assert "abcdefghijklmnopqrstuvwxyz" not in masked
    assert "…" in masked or "***" in masked
