"""A plan an admin assigned by hand (manual override / Enterprise) is never switched by Stripe
events. Regression: an old Stripe test subscription renewed and dropped the admin account from
Enterprise to Starter, locking campaigns, blasts and knowledge bases."""
import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

SUB = {
    "id": "sub_1", "customer": "cus_1", "status": "active", "cancel_at_period_end": False,
    "current_period_start": 1_790_000_000, "current_period_end": 1_792_600_000,
    "items": {"data": [{"price": {"id": "price_starter_test", "product": "prod_s"}}]},
}


@pytest.fixture
def stripe_test_prices(monkeypatch):
    from app.billing import stripe_config
    from app.config import settings

    monkeypatch.setattr(settings, "STRIPE_MODE", "test")
    monkeypatch.setattr(settings, "STRIPE_TEST_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_TEST_PRICE_STARTER", "price_starter_test")
    stripe_config.clear_stripe_runtime_cache()
    yield
    stripe_config.clear_stripe_runtime_cache()


async def _user(db, **fields):
    uid = (await db.users.insert_one({"email": "admin@example.com", "stripe_customer_id": "cus_1",
                                      "stripe_subscription_id": "sub_1", **fields})).inserted_id
    return uid


@pytest.mark.asyncio
@pytest.mark.parametrize("start", [
    {"plan": "enterprise", "subscription_status": "manual_override"},
    {"plan": "enterprise", "subscription_status": "active"},          # Enterprise is never Stripe-backed
    {"plan": "business", "subscription_status": "manual_override"},   # any plan set by hand in Admin
])
async def test_stripe_renewal_never_switches_a_hand_assigned_plan(stripe_test_prices, start):
    from app.routes import billing

    db = AsyncMongoMockClient().db
    uid = await _user(db, **start)
    await billing._apply_subscription(db, {**SUB, "metadata": {"user_id": str(uid)}})
    u = await db.users.find_one({"_id": uid})
    assert u["plan"] == start["plan"] and u["subscription_status"] == start["subscription_status"]
    assert u["stripe_price_id"] == "price_starter_test" and u["current_period_end"] is not None  # details still recorded


@pytest.mark.asyncio
async def test_cancelled_test_subscription_does_not_drop_a_manual_plan_to_free(stripe_test_prices):
    from app.routes import billing

    db = AsyncMongoMockClient().db
    uid = await _user(db, plan="enterprise", subscription_status="manual_override")
    await billing._apply_subscription(db, {"id": "sub_1", "customer": "cus_1", "canceled_at": 1_792_000_000,
                                           "metadata": {"user_id": str(uid)}}, deleted=True)
    u = await db.users.find_one({"_id": uid})
    assert u["plan"] == "enterprise" and u["subscription_status"] == "manual_override"
    assert u.get("cancelled_at") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("payment", ["paid", "failed"])
async def test_invoices_record_payment_but_keep_the_manual_plan(payment):
    from app.routes import billing

    db = AsyncMongoMockClient().db
    uid = await _user(db, plan="enterprise", subscription_status="manual_override")
    await billing._handle_invoice(db, {"id": "in_1", "customer": "cus_1", "subscription": "sub_1",
                                       "metadata": {"user_id": str(uid)}, "status_transitions": {"paid_at": 1_792_000_000}},
                                  payment_status=payment)
    u = await db.users.find_one({"_id": uid})
    assert u["subscription_status"] == "manual_override" and u["last_payment_status"] == payment


@pytest.mark.asyncio
async def test_checkout_does_not_switch_a_manual_plan():
    from app.routes import billing

    db = AsyncMongoMockClient().db
    uid = await _user(db, plan="enterprise", subscription_status="manual_override")
    await billing._handle_checkout_completed(db, {"client_reference_id": str(uid), "customer": "cus_1", "subscription": "sub_2",
                                                  "metadata": {"user_id": str(uid), "app_plan": "starter"}})
    u = await db.users.find_one({"_id": uid})
    assert u["plan"] == "enterprise" and u["subscription_status"] == "manual_override"


@pytest.mark.asyncio
async def test_normal_paying_customers_still_follow_stripe(stripe_test_prices):
    from app.routes import billing

    db = AsyncMongoMockClient().db
    uid = await _user(db, plan="free", subscription_status="none")
    await billing._apply_subscription(db, {**SUB, "metadata": {"user_id": str(uid)}})
    assert (await db.users.find_one({"_id": uid}))["plan"] == "starter"
    await billing._apply_subscription(db, {"id": "sub_1", "customer": "cus_1", "metadata": {"user_id": str(uid)}}, deleted=True)
    u = await db.users.find_one({"_id": uid})
    assert u["plan"] == "free" and u["subscription_status"] == "canceled"


@pytest.mark.asyncio
async def test_billing_page_says_when_the_plan_is_assigned_by_admin():
    from app.routes import billing

    manual = await billing.get_subscription(user={"_id": ObjectId(), "plan": "enterprise", "subscription_status": "manual_override"})
    paying = await billing.get_subscription(user={"_id": ObjectId(), "plan": "starter", "subscription_status": "active"})
    assert manual["manual_plan"] is True and manual["plan_name"] == "Enterprise"
    assert paying["manual_plan"] is False
