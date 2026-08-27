"""Unit tests for plan entitlement enforcement."""
import pytest
from fastapi import HTTPException

from app.services import entitlements as ent


def _user(plan="free", status="none", **extra):
    return {"_id": "u1", "plan": plan, "subscription_status": status, **extra}


# --- effective plan resolution ----------------------------------------------------

def test_free_user_is_free():
    assert ent.effective_plan_key(_user("free", "none")) == "free"


def test_active_paid_plan_applies():
    assert ent.effective_plan_key(_user("professional", "active")) == "professional"


def test_trialing_and_past_due_still_entitled():
    assert ent.effective_plan_key(_user("starter", "trialing")) == "starter"
    assert ent.effective_plan_key(_user("business", "past_due")) == "business"


def test_manual_override_is_entitled():
    assert ent.effective_plan_key(_user("business", "manual_override")) == "business"


def test_enterprise_always_entitled_without_subscription():
    # Enterprise is contact-sales / manually assigned — no Stripe subscription.
    assert ent.effective_plan_key(_user("enterprise", "none")) == "enterprise"
    assert ent.effective_plan_key(_user("enterprise", None)) == "enterprise"
    assert ent.effective_entitlements(_user("enterprise", "none"))["ai_conversations_month"] == -1


def test_paid_plan_without_active_subscription_falls_back_to_free():
    # e.g. incomplete/canceled/none -> not entitled
    assert ent.effective_plan_key(_user("professional", "incomplete")) == "free"
    assert ent.effective_plan_key(_user("professional", "none")) == "free"


# --- boolean feature gates --------------------------------------------------------

def test_campaigns_blocked_on_starter_allowed_on_professional():
    with pytest.raises(HTTPException) as exc:
        ent.require_feature(_user("starter", "active"), "campaigns", label="Campaigns")
    assert exc.value.status_code == 402
    assert exc.value.detail["code"] == "entitlement_required"
    # professional includes campaigns
    ent.require_feature(_user("professional", "active"), "campaigns", label="Campaigns")


def test_broadcast_blocked_on_free():
    with pytest.raises(HTTPException):
        ent.require_feature(_user("free", "none"), "whatsapp_broadcast", label="Broadcast")


# --- counted capacity gates -------------------------------------------------------

def test_ai_agents_capacity_starter_one():
    u = _user("starter", "active")  # ai_agents = 1
    ent.require_capacity(u, "ai_agents", 0, label="AI agents")  # first agent ok
    with pytest.raises(HTTPException) as exc:
        ent.require_capacity(u, "ai_agents", 1, label="AI agents")  # second blocked
    assert exc.value.detail["limit"] == 1
    assert exc.value.detail["current"] == 1


def test_ai_agents_unlimited_on_business():
    u = _user("business", "active")  # ai_agents = -1
    ent.require_capacity(u, "ai_agents", 9999, label="AI agents")  # never raises


def test_free_cannot_create_agent():
    with pytest.raises(HTTPException):
        ent.require_capacity(_user("free", "none"), "ai_agents", 0, label="AI agents")


# --- number connection gate -------------------------------------------------------

def test_free_cannot_connect_number():
    with pytest.raises(HTTPException) as exc:
        ent.require_can_connect_number(_user("free", "none"))
    assert exc.value.detail["entitlement"] == "whatsapp_numbers"


def test_paid_can_connect_number():
    ent.require_can_connect_number(_user("starter", "active"))  # no raise


def test_downgraded_paid_plan_cannot_connect():
    # professional plan value but no active subscription -> effective free
    with pytest.raises(HTTPException):
        ent.require_can_connect_number(_user("professional", "canceled"))


def test_connected_number_count():
    assert ent.connected_number_count({"twilio_whatsapp_to": "+441234567890"}) == 1
    assert ent.connected_number_count({"meta_connection_status": "connected"}) == 1
    assert (
        ent.connected_number_count(
            {"twilio_whatsapp_to": "+441234567890", "meta_connection_status": "connected"}
        )
        == 2
    )
    assert ent.connected_number_count({}) == 0


# --- monthly AI-conversation meter (fake redis) -----------------------------------

class FakeRedis:
    def __init__(self):
        self.sets: dict[str, set] = {}

    def sismember(self, key, member):
        return member in self.sets.get(key, set())

    def scard(self, key):
        return len(self.sets.get(key, set()))

    def pipeline(self):
        return _FakePipe(self)


class _FakePipe:
    def __init__(self, r):
        self.r = r
        self.ops = []

    def sadd(self, key, member):
        self.ops.append(("sadd", key, member))
        return self

    def expire(self, key, ttl):
        self.ops.append(("expire", key, ttl))
        return self

    def execute(self):
        for op in self.ops:
            if op[0] == "sadd":
                self.r.sets.setdefault(op[1], set()).add(op[2])
        self.ops = []


def test_ai_conversation_meter_counts_distinct_leads():
    r = FakeRedis()
    tenant, limit = "t1", 2
    # lead A allowed + recorded
    assert ent.ai_conversation_allowed(r, tenant, "A", limit)
    ent.record_ai_conversation(r, tenant, "A")
    # same lead again -> still allowed, no extra consumption
    assert ent.ai_conversation_allowed(r, tenant, "A", limit)
    ent.record_ai_conversation(r, tenant, "A")
    assert ent.ai_conversation_count(r, tenant) == 1
    # second distinct lead allowed
    assert ent.ai_conversation_allowed(r, tenant, "B", limit)
    ent.record_ai_conversation(r, tenant, "B")
    assert ent.ai_conversation_count(r, tenant) == 2
    # third distinct lead blocked (limit 2 reached)
    assert not ent.ai_conversation_allowed(r, tenant, "C", limit)


def test_ai_conversation_limit_zero_blocks_all():
    r = FakeRedis()
    assert not ent.ai_conversation_allowed(r, "t", "A", 0)


def test_ai_conversation_limit_unlimited():
    r = FakeRedis()
    assert ent.ai_conversation_allowed(r, "t", "A", -1)


def test_ai_conversation_fails_open_on_redis_error():
    class BadRedis:
        def sismember(self, *a):
            raise RuntimeError("down")

        def scard(self, *a):
            raise RuntimeError("down")

    assert ent.ai_conversation_allowed(BadRedis(), "t", "A", 1) is True


# --- usage snapshot uses effective plan ------------------------------------------

def test_effective_entitlements_free_for_inactive_paid():
    e = ent.effective_entitlements(_user("business", "incomplete"))
    assert e["campaigns"] is False
    assert e["ai_agents"] == 0
