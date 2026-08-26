"""Phase 2B Meta AI reply tests (no live OpenAI, Graph, or Twilio)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId

from fastapi.testclient import TestClient

from app.main import app
from app.services.meta_whatsapp_service import MetaWhatsAppError
from app.services.whatsapp_outbound import UnknownWhatsAppProviderError, send_whatsapp_text
from app.workers import tasks
from tests.test_meta_inbound_phase2a import (
    MemDB,
    _meta_payload,
    _noop_lifespan,
    _post_meta,
    _user,
    patched_meta,
)


@pytest.fixture
def client():
    app.router.lifespan_context = _noop_lifespan
    with TestClient(app) as c:
        yield c


@pytest.fixture
def pushes():
    items: list[tuple] = []

    async def _push(user_id, event, data):
        items.append((str(user_id), event, data))

    return items, _push


@pytest.fixture
def mem():
    return MemDB()


def _open_lead(user_id: str, lead_id: ObjectId, **extra) -> dict:
    now = datetime.now(timezone.utc)
    doc = {
        "_id": lead_id,
        "user_id": user_id,
        "phone": "+447700900123",
        "blacklisted": False,
        "ai_paused": False,
        "takeover_by": None,
        "whatsapp_consent_status": "opted_in",
        "last_inbound_at": now - timedelta(hours=1),
        "whatsapp_window_expires_at": now + timedelta(hours=23),
    }
    doc.update(extra)
    return doc


def _ai_settings(**extra):
    cfg = {
        "enabled": True,
        "moderation_enabled": False,
        "extraction_enabled": False,
        "summaries_enabled": False,
        "ai_disallowed_topics": "",
        "model": "gpt-4o-mini",
        "fallback_model": "gpt-4o-mini",
        "temperature": 0.5,
        "max_output_tokens": 400,
    }
    cfg.update(extra)
    return cfg


def _run_ai_job(
    db,
    user_id,
    lead_id,
    *,
    provider=None,
    trigger_message_id=None,
    openai_reply="AI hello",
    openai_exc=None,
    send_result=None,
    send_exc=None,
    claim=True,
    permit=True,
    elig=None,
):
    publishes = []

    def _pub(uid, event, data):
        publishes.append((uid, event, data))

    send_result = send_result or {
        "provider": provider or "twilio",
        "provider_message_id": "wamid.OUT" if provider == "meta" else "SMxxx",
        "status": "sent",
    }
    elig = elig or SimpleNamespace(
        allowed=True,
        reason_code="ok",
        safe_message="",
        consent_status="opted_in",
        window_status="open",
    )
    openai = MagicMock(side_effect=openai_exc) if openai_exc else MagicMock(return_value=openai_reply)
    send = MagicMock(side_effect=send_exc) if send_exc else MagicMock(return_value=send_result)
    twilio_send = MagicMock(return_value={"sid": "SMtwilio", "status": "queued"})
    meta_send = MagicMock(
        return_value=SimpleNamespace(provider="meta", provider_message_id="wamid.GRAPH")
    )
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "_publish", _pub),
        patch.object(tasks, "claim_idempotency", return_value=claim),
        patch.object(tasks, "acquire_send_permit", return_value=permit),
        patch.object(tasks, "release_send_permit"),
        patch.object(tasks, "get_whatsapp_send_eligibility", return_value=elig),
        patch.object(tasks, "send_whatsapp_text", send),
        patch.object(tasks.twilio_service, "send_whatsapp", twilio_send),
        patch("app.services.ai_quota.check_quota", return_value=(True, None)),
        patch("app.services.ai_config.resolve_ai_settings", return_value=_ai_settings()),
        patch("app.services.ai_summary.get_summary", return_value=None),
        patch("app.services.ai_summary.maybe_enqueue_summary"),
        patch(
            "app.services.ai_context.load_conversation_context",
            return_value={"messages": [{"role": "user", "content": "hi"}], "summary_used": False},
        ),
        patch.object(tasks.openai_service, "generate_reply", openai),
        patch("app.services.whatsapp_outbound.meta_whatsapp_service.send_text", meta_send),
    ):
        tasks.generate_and_send_ai_reply(
            user_id,
            str(lead_id) if not isinstance(lead_id, str) else lead_id,
            provider=provider,
            trigger_message_id=trigger_message_id,
        )
    return {
        "openai": openai,
        "send": send,
        "twilio": twilio_send,
        "meta": meta_send,
        "publishes": publishes,
    }


def test_meta_inbound_enqueues_ai_with_provider_and_trigger(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    enq = MagicMock()
    with patched_meta(mem, push, enqueue=enq), patch(
        "app.config.settings.OPENAI_API_KEY", "sk-test"
    ), patch("app.config.settings.AI_FEATURES_ENABLED", True):
        res = _post_meta(client, _meta_payload(phone_number_id="PN_A", wamid="wamid.P2B1"))
    assert res.status_code == 200
    ai_calls = [
        c for c in enq.call_args_list if c.args and c.args[0] is tasks.generate_and_send_ai_reply
    ]
    assert len(ai_calls) == 1
    assert ai_calls[0].kwargs["provider"] == "meta"
    trigger = ai_calls[0].kwargs["trigger_message_id"]
    assert trigger == str(mem.messages.docs[0]["_id"])
    welcome = [c for c in enq.call_args_list if c.args and c.args[0] is tasks.send_welcome_and_terms]
    assert welcome == []


def test_meta_stop_does_not_enqueue_ai(client, mem, pushes):
    items, push = pushes
    mem.users.docs.append(_user("PN_A"))
    enq = MagicMock()
    with patched_meta(mem, push, enqueue=enq), patch(
        "app.config.settings.OPENAI_API_KEY", "sk-test"
    ):
        _post_meta(
            client,
            _meta_payload(phone_number_id="PN_A", wamid="wamid.STOP1", body="STOP"),
        )
    ai_calls = [
        c for c in enq.call_args_list if c.args and c.args[0] is tasks.generate_and_send_ai_reply
    ]
    assert ai_calls == []


def test_worker_uses_trigger_provider_not_latest_inbound():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    trigger_id = ObjectId()
    later_id = ObjectId()
    trigger = {
        "_id": trigger_id,
        "user_id": user_id,
        "lead_id": str(lead_id),
        "direction": "inbound",
        "provider": "meta",
        "message": "from meta",
    }
    later = {
        "_id": later_id,
        "user_id": user_id,
        "lead_id": str(lead_id),
        "direction": "inbound",
        "provider": "twilio",
        "message": "from twilio later",
    }
    outbound_id = ObjectId()
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, lead_id))
    db.users.find_one = MagicMock(
        return_value={"_id": ObjectId(user_id), "meta_phone_number_id": "PN_A", "plan": "business", "subscription_status": "active"}
    )
    db.agents.find_one = MagicMock(return_value={"_id": ObjectId(), "name": "Agent"})
    db.messages.count_documents = MagicMock(return_value=3)
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=outbound_id))
    db.messages.update_one = MagicMock()
    db.leads.update_one = MagicMock()

    def find_one(query, *args, **kwargs):
        if query.get("_id") == trigger_id:
            return trigger
        if query.get("direction") == "inbound":
            return later
        if query.get("_id") == outbound_id:
            return {"_id": outbound_id, "provider": "meta", "status": "sent"}
        return None

    db.messages.find_one = MagicMock(side_effect=find_one)
    result = _run_ai_job(
        db,
        user_id,
        lead_id,
        provider="meta",
        trigger_message_id=str(trigger_id),
        send_result={"provider": "meta", "provider_message_id": "wamid.OUT", "status": "sent"},
    )
    result["openai"].assert_called_once()
    result["send"].assert_called_once()
    assert result["send"].call_args.kwargs["provider"] == "meta"
    result["twilio"].assert_not_called()
    inserted = db.messages.insert_one.call_args[0][0]
    assert inserted["provider"] == "meta"
    assert inserted["twilio_sid"] is None
    assert inserted["trigger_message_id"] == str(trigger_id)
    assert inserted["sender_type"] == "ai"
    assert result["send"].call_args.kwargs["provider"] == "meta"
    events = [e for _, e, _ in result["publishes"]]
    assert "message:new" in events
    assert "message:updated" in events
    set_fields = db.messages.update_one.call_args[0][1]["$set"]
    assert set_fields["provider"] == "meta"
    assert set_fields["provider_message_id"] == "wamid.OUT"
    assert "twilio_sid" not in set_fields


def test_graph_hard_failure_marks_failed_no_twilio_fallback():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    trigger_id = ObjectId()
    outbound_id = ObjectId()
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, lead_id))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.agents.find_one = MagicMock(return_value=None)
    db.messages.count_documents = MagicMock(return_value=2)
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=outbound_id))
    db.messages.update_one = MagicMock()
    db.messages.find_one = MagicMock(
        return_value={
            "_id": trigger_id,
            "user_id": user_id,
            "lead_id": str(lead_id),
            "direction": "inbound",
            "provider": "meta",
            "message": "hi",
        }
    )
    db.leads.update_one = MagicMock()
    result = _run_ai_job(
        db,
        user_id,
        lead_id,
        provider="meta",
        trigger_message_id=str(trigger_id),
        send_exc=MetaWhatsAppError("Graph rejected", status_code=400),
    )
    result["twilio"].assert_not_called()
    fail_set = db.messages.update_one.call_args[0][1]["$set"]
    assert fail_set["status"] == "failed"


def test_ai_pause_and_takeover_prevent_send():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    trigger_id = ObjectId()
    trigger = {
        "_id": trigger_id,
        "user_id": user_id,
        "lead_id": str(lead_id),
        "direction": "inbound",
        "provider": "meta",
        "message": "hi",
    }
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, lead_id, ai_paused=True))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.messages.find_one = MagicMock(return_value=trigger)
    result = _run_ai_job(
        db, user_id, lead_id, provider="meta", trigger_message_id=str(trigger_id)
    )
    result["openai"].assert_not_called()
    result["send"].assert_not_called()

    db.leads.find_one = MagicMock(
        return_value=_open_lead(user_id, lead_id, takeover_by="agent-1", ai_paused=False)
    )
    result = _run_ai_job(db, user_id, lead_id, provider="twilio")
    result["openai"].assert_not_called()
    result["send"].assert_not_called()


def test_opted_out_and_blacklist_prevent_meta_send():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    trigger_id = ObjectId()
    trigger = {
        "_id": trigger_id,
        "user_id": user_id,
        "lead_id": str(lead_id),
        "direction": "inbound",
        "provider": "meta",
        "message": "hi",
    }
    db = MagicMock()
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.messages.find_one = MagicMock(return_value=trigger)
    db.agents.find_one = MagicMock(return_value=None)

    opted = _open_lead(user_id, lead_id, whatsapp_consent_status="opted_out")
    db.leads.find_one = MagicMock(return_value=opted)
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "send_whatsapp_text") as send,
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks.openai_service, "generate_reply") as openai,
        patch("app.services.ai_config.resolve_ai_settings", return_value=_ai_settings()),
        patch("app.services.ai_quota.check_quota", return_value=(True, None)),
        patch("app.services.whatsapp_eligibility.settings.META_ACCESS_TOKEN", "tok"),
        patch("app.services.whatsapp_eligibility.settings.META_PHONE_NUMBER_ID", "PN_A"),
        patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True),
    ):
        tasks.generate_and_send_ai_reply(
            user_id, str(lead_id), provider="meta", trigger_message_id=str(trigger_id)
        )
    openai.assert_not_called()
    send.assert_not_called()
    twilio.assert_not_called()

    blocked = _open_lead(user_id, lead_id, blacklisted=True)
    db.leads.find_one = MagicMock(return_value=blocked)
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "send_whatsapp_text") as send,
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks.openai_service, "generate_reply") as openai,
        patch("app.services.ai_config.resolve_ai_settings", return_value=_ai_settings()),
        patch("app.services.ai_quota.check_quota", return_value=(True, None)),
        patch("app.services.whatsapp_eligibility.settings.META_ACCESS_TOKEN", "tok"),
        patch("app.services.whatsapp_eligibility.settings.META_PHONE_NUMBER_ID", "PN_A"),
        patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True),
    ):
        tasks.generate_and_send_ai_reply(
            user_id, str(lead_id), provider="meta", trigger_message_id=str(trigger_id)
        )
    openai.assert_not_called()
    send.assert_not_called()
    twilio.assert_not_called()


def test_closed_window_prevents_meta_freeform_and_twilio_template():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    trigger_id = ObjectId()
    now = datetime.now(timezone.utc)
    lead = _open_lead(
        user_id,
        lead_id,
        last_inbound_at=now - timedelta(hours=30),
        whatsapp_window_expires_at=now - timedelta(hours=1),
    )
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=lead)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.messages.find_one = MagicMock(
        return_value={
            "_id": trigger_id,
            "user_id": user_id,
            "lead_id": str(lead_id),
            "direction": "inbound",
            "provider": "meta",
        }
    )
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "send_whatsapp_text") as send,
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks.openai_service, "generate_reply") as openai,
        patch("app.services.ai_config.resolve_ai_settings", return_value=_ai_settings()),
        patch("app.services.ai_quota.check_quota", return_value=(True, None)),
        patch("app.services.whatsapp_eligibility.settings.META_ACCESS_TOKEN", "tok"),
        patch("app.services.whatsapp_eligibility.settings.META_PHONE_NUMBER_ID", "PN_A"),
        patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True),
    ):
        tasks.generate_and_send_ai_reply(
            user_id, str(lead_id), provider="meta", trigger_message_id=str(trigger_id)
        )
    openai.assert_not_called()
    send.assert_not_called()
    twilio.assert_not_called()
    assert twilio.call_args_list == []


def test_legacy_two_arg_twilio_job_still_calls_twilio_not_meta():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    outbound_id = ObjectId()
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, lead_id))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.agents.find_one = MagicMock(return_value=None)
    db.messages.find_one = MagicMock(return_value={"message": "hi", "direction": "inbound"})
    db.messages.count_documents = MagicMock(return_value=2)
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=outbound_id))
    db.messages.update_one = MagicMock()
    db.leads.update_one = MagicMock()
    result = _run_ai_job(
        db,
        user_id,
        lead_id,
        send_result={"provider": "twilio", "provider_message_id": "SMxxx", "status": "queued"},
    )
    result["openai"].assert_called_once()
    result["send"].assert_called_once()
    assert result["send"].call_args.kwargs["provider"] == "twilio"
    result["meta"].assert_not_called()


def test_new_twilio_job_never_calls_meta():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    trigger_id = ObjectId()
    outbound_id = ObjectId()
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, lead_id))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.agents.find_one = MagicMock(return_value=None)
    db.messages.count_documents = MagicMock(return_value=2)
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=outbound_id))
    db.messages.update_one = MagicMock()
    db.leads.update_one = MagicMock()
    db.messages.find_one = MagicMock(
        return_value={
            "_id": trigger_id,
            "user_id": user_id,
            "lead_id": str(lead_id),
            "direction": "inbound",
            "provider": "twilio",
            "message": "hi",
        }
    )
    result = _run_ai_job(
        db,
        user_id,
        lead_id,
        provider="twilio",
        trigger_message_id=str(trigger_id),
        send_result={"provider": "twilio", "provider_message_id": "SMnew", "status": "queued"},
    )
    assert result["send"].call_args.kwargs["provider"] == "twilio"
    result["meta"].assert_not_called()
    set_fields = db.messages.update_one.call_args[0][1]["$set"]
    assert set_fields.get("twilio_sid") == "SMnew"


def test_same_trigger_does_not_double_send():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    trigger_id = ObjectId()
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, lead_id))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.messages.find_one = MagicMock(
        return_value={
            "_id": trigger_id,
            "user_id": user_id,
            "lead_id": str(lead_id),
            "direction": "inbound",
            "provider": "meta",
        }
    )
    result = _run_ai_job(
        db,
        user_id,
        lead_id,
        provider="meta",
        trigger_message_id=str(trigger_id),
        claim=False,
    )
    result["openai"].assert_not_called()
    result["send"].assert_not_called()


def test_provider_conflict_and_unknown_provider_fail_safe():
    user_id = str(ObjectId())
    lead_id = ObjectId()
    trigger_id = ObjectId()
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, lead_id))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.messages.find_one = MagicMock(
        return_value={
            "_id": trigger_id,
            "user_id": user_id,
            "lead_id": str(lead_id),
            "direction": "inbound",
            "provider": "meta",
        }
    )
    result = _run_ai_job(
        db,
        user_id,
        lead_id,
        provider="twilio",
        trigger_message_id=str(trigger_id),
    )
    result["openai"].assert_not_called()
    result["send"].assert_not_called()
    result["twilio"].assert_not_called()

    with pytest.raises(UnknownWhatsAppProviderError):
        send_whatsapp_text(provider="pagerduty", to="+447700900123", text="hi")


def test_meta_retry_path_stays_on_meta():
    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    message_id = str(ObjectId())
    db = MagicMock()
    db.messages.find_one = MagicMock(
        return_value={
            "_id": ObjectId(message_id),
            "user_id": user_id,
            "provider": "meta",
            "message": "AI hello",
            "status": "queued",
            "message_purpose": "support",
        }
    )
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, ObjectId(lead_id)))
    db.users.find_one = MagicMock(
        return_value={"_id": ObjectId(user_id), "meta_phone_number_id": "PN_A", "plan": "business", "subscription_status": "active"}
    )
    db.messages.update_one = MagicMock()
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(tasks, "release_send_permit"),
        patch.object(
            tasks,
            "get_whatsapp_send_eligibility",
            return_value=SimpleNamespace(
                allowed=True,
                reason_code="ok",
                safe_message="",
                consent_status="opted_in",
                window_status="open",
            ),
        ),
        patch.object(
            tasks,
            "send_whatsapp_text",
            return_value={"provider": "meta", "provider_message_id": "wamid.R", "status": "sent"},
        ) as send,
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "_publish"),
    ):
        tasks.send_outbound_message(message_id, user_id, lead_id, body="AI hello")
    send.assert_called_once()
    assert send.call_args.kwargs["provider"] == "meta"
    twilio.assert_not_called()
    set_fields = db.messages.update_one.call_args[0][1]["$set"]
    assert set_fields["provider"] == "meta"
    assert set_fields["provider_message_id"] == "wamid.R"
    assert "twilio_sid" not in set_fields


def test_meta_retry_rejects_media_and_templates():
    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    message_id = str(ObjectId())
    db = MagicMock()
    db.messages.find_one = MagicMock(
        return_value={
            "_id": ObjectId(message_id),
            "user_id": user_id,
            "provider": "meta",
            "status": "queued",
            "content_sid": "HXabc",
        }
    )
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, ObjectId(lead_id)))
    db.messages.update_one = MagicMock()
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "send_whatsapp_text") as send,
        patch.object(tasks, "_publish"),
    ):
        tasks.send_outbound_message(
            message_id, user_id, lead_id, body="hi", content_sid="HXabc"
        )
    send.assert_not_called()
    twilio.assert_not_called()
    assert db.messages.update_one.call_args[0][1]["$set"]["status"] == "failed"


def test_pnid_mismatch_does_not_send():
    user = {"meta_phone_number_id": "PN_TENANT"}
    with (
        patch(
            "app.services.meta_credentials.get_meta_credentials_for_user",
            side_effect=__import__(
                "app.services.meta_credentials", fromlist=["MetaCredentialsError"]
            ).MetaCredentialsError("Meta credential phone number ID does not match this account"),
        ),
        patch("app.services.meta_whatsapp_service.httpx.Client") as client,
        pytest.raises(MetaWhatsAppError, match="does not match"),
    ):
        send_whatsapp_text(provider="meta", to="+447700900123", text="hi", user=user)
    client.assert_not_called()


def test_whatsapp_provider_env_is_not_used_for_legacy_twilio_job(monkeypatch):
    monkeypatch.setattr(tasks.settings, "WHATSAPP_PROVIDER", "meta")
    user_id = str(ObjectId())
    lead_id = ObjectId()
    outbound_id = ObjectId()
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=_open_lead(user_id, lead_id))
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "plan": "business", "subscription_status": "active"})
    db.agents.find_one = MagicMock(return_value=None)
    db.messages.find_one = MagicMock(return_value={"message": "hi", "direction": "inbound"})
    db.messages.count_documents = MagicMock(return_value=2)
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=outbound_id))
    db.messages.update_one = MagicMock()
    db.leads.update_one = MagicMock()
    result = _run_ai_job(db, user_id, lead_id)
    assert result["send"].call_args.kwargs["provider"] == "twilio"


def test_blacklisted_inbound_does_not_enqueue_ai(client, mem, pushes):
    items, push = pushes
    user = _user("PN_A")
    mem.users.docs.append(user)
    mem.blacklist.docs.append({"user_id": str(user["_id"]), "phone": "+447700900123"})
    enq = MagicMock()
    with patched_meta(mem, push, enqueue=enq), patch(
        "app.config.settings.OPENAI_API_KEY", "sk-test"
    ):
        _post_meta(client, _meta_payload(phone_number_id="PN_A", wamid="wamid.BL1"))
    ai_calls = [
        c for c in enq.call_args_list if c.args and c.args[0] is tasks.generate_and_send_ai_reply
    ]
    assert ai_calls == []
