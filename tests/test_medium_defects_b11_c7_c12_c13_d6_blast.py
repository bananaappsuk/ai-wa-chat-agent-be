"""Tests for the medium-priority defect fixes:

B11 (welcome enqueue), C7 (self-sender loop guard), C12/C13 (AI truncated /
empty response handling), D6 (unique phone race + duplicate reporting), and
blast reliability parity (batch engine, retries, pause/cancel).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError


# ---------------------------------------------------------------------------
# B11: welcome + terms enqueue
# ---------------------------------------------------------------------------


def test_send_welcome_and_terms_skips_if_already_sent():
    from app.workers import tasks

    db = MagicMock()
    db.leads.find_one = MagicMock(
        return_value={
            "_id": ObjectId(),
            "user_id": "u1",
            "phone": "+447700900000",
            "welcome_sent_at": datetime.now(timezone.utc),
        }
    )
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks.twilio_service, "send_whatsapp") as send,
    ):
        tasks.send_welcome_and_terms("u1", str(ObjectId()))
    send.assert_not_called()


def test_send_welcome_and_terms_respects_idempotency_claim():
    from app.workers import tasks

    db = MagicMock()
    db.leads.find_one = MagicMock(
        return_value={"_id": ObjectId(), "user_id": "u1", "phone": "+447700900000"}
    )
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=False),
        patch.object(tasks.twilio_service, "send_whatsapp") as send,
    ):
        tasks.send_welcome_and_terms("u1", str(ObjectId()))
    send.assert_not_called()


def test_send_welcome_and_terms_sends_welcome_and_terms_and_marks_sent():
    from app.workers import tasks

    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    lead = {
        "_id": ObjectId(lead_id),
        "user_id": user_id,
        "phone": "+447700900000",
        "whatsapp_consent_status": "opted_in",
        "last_inbound_at": datetime.now(timezone.utc),
    }
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=lead)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id)})
    db.agents.find_one = MagicMock(
        return_value={"welcome_message": "Welcome!", "terms_text": "T&Cs apply"}
    )
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=ObjectId()))

    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks, "_publish"),
        patch.object(
            tasks.twilio_service,
            "send_whatsapp",
            return_value={"sid": "SMx", "status": "queued"},
        ) as send,
    ):
        tasks.send_welcome_and_terms(user_id, lead_id)

    assert send.call_count == 2
    assert db.messages.insert_one.call_count == 2
    final_set = db.leads.update_one.call_args_list[-1][0][1]["$set"]
    assert final_set["welcome_terms_status"] == "sent"
    assert "welcome_sent_at" in final_set


def test_send_welcome_and_terms_blocked_when_ineligible():
    from app.workers import tasks

    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    lead = {
        "_id": ObjectId(lead_id),
        "user_id": user_id,
        "phone": "+447700900000",
        "blacklisted": True,
    }
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=lead)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id)})
    db.agents.find_one = MagicMock(return_value={"welcome_message": "Welcome!", "terms_text": ""})

    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks.twilio_service, "send_whatsapp") as send,
    ):
        tasks.send_welcome_and_terms(user_id, lead_id)

    send.assert_not_called()
    final_set = db.leads.update_one.call_args_list[-1][0][1]["$set"]
    assert final_set["welcome_terms_status"] == "blocked"


def test_send_welcome_and_terms_records_activity_on_failure():
    from app.workers import tasks

    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    lead = {
        "_id": ObjectId(lead_id),
        "user_id": user_id,
        "phone": "+447700900000",
        "whatsapp_consent_status": "opted_in",
        "last_inbound_at": datetime.now(timezone.utc),
    }
    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=lead)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id)})
    db.agents.find_one = MagicMock(return_value={"welcome_message": "Welcome!", "terms_text": ""})

    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch.object(tasks.twilio_service, "send_whatsapp", side_effect=RuntimeError("boom")),
        patch("app.services.activity.record_activity_sync") as rec,
    ):
        tasks.send_welcome_and_terms(user_id, lead_id)

    final_set = db.leads.update_one.call_args_list[-1][0][1]["$set"]
    assert final_set["welcome_terms_status"] == "failed"
    assert final_set.get("welcome_error")
    rec.assert_called_once()
    assert rec.call_args.kwargs["event_type"] == "welcome.send_failed"


# ---------------------------------------------------------------------------
# C7: self-sender loop guard
# ---------------------------------------------------------------------------


def test_is_self_sender_matches_twilio_from():
    from app.services.phone_norm import is_self_sender
    from app.config import settings

    assert is_self_sender(settings.TWILIO_WHATSAPP_FROM, None) is True
    assert is_self_sender("whatsapp:+14155238886", {}) is True


def test_is_self_sender_matches_user_configured_number():
    from app.services.phone_norm import is_self_sender

    user = {"twilio_whatsapp_to": "+447700900999"}
    assert is_self_sender("07700900999", user) is True


def test_is_self_sender_false_for_other_numbers():
    from app.services.phone_norm import is_self_sender

    user = {"twilio_whatsapp_to": "+447700900999"}
    assert is_self_sender("+447700900123", user) is False
    assert is_self_sender(None, user) is False


class _FakeURL:
    path = "/api/webhook/whatsapp"
    scheme = "https"
    netloc = "example.com"


class _FakeRequest:
    def __init__(self, data: dict):
        self._data = data
        self.headers: dict = {}
        self.url = _FakeURL()

    async def form(self):
        return dict(self._data)


@pytest.mark.asyncio
async def test_webhook_ignores_self_sender_and_skips_lead_creation():
    from app.routes import webhook as wh
    from app.config import settings as app_settings

    user_id = ObjectId()
    form_data = {
        "MessageSid": "SM_selfsender_1",
        "From": app_settings.TWILIO_WHATSAPP_FROM,
        "To": "whatsapp:+447700900999",
        "Body": "hi",
    }

    db = MagicMock()
    db.webhook_events.find_one = AsyncMock(return_value=None)
    db.webhook_events.insert_one = AsyncMock()
    db.users.find_one = AsyncMock(return_value={"_id": user_id, "twilio_whatsapp_to": "+447700900999"})
    db.activity_events.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))

    with (
        patch.object(wh, "get_db", return_value=db),
        patch("app.security.rate_limit.rate_limit_webhook"),
        patch.object(wh.twilio_service, "validate_signature", return_value=True),
        patch("app.services.inbound_whatsapp.recalculate_lead_score", new=AsyncMock()),
    ):
        resp = await wh.whatsapp_webhook(_FakeRequest(form_data))

    assert resp.status_code == 200
    db.leads.find_one.assert_not_called()
    db.messages.insert_one.assert_not_called()
    db.activity_events.insert_one.assert_called_once()
    assert db.activity_events.insert_one.call_args[0][0]["event_type"] == "self_sender_ignored"


@pytest.mark.asyncio
async def test_webhook_processes_normal_inbound_when_not_self_sender():
    from app.routes import webhook as wh
    from app.config import settings as app_settings

    user_id = ObjectId()
    lead_id = ObjectId()
    form_data = {
        "MessageSid": "SM_normal_1",
        "From": "whatsapp:+447700900123",
        "To": "whatsapp:+447700900999",
        "Body": "hello there",
    }

    db = MagicMock()
    db.webhook_events.find_one = AsyncMock(return_value=None)
    db.webhook_events.insert_one = AsyncMock()
    db.users.find_one = AsyncMock(return_value={"_id": user_id, "twilio_whatsapp_to": "+447700900999"})
    db.blacklist.find_one = AsyncMock(return_value=None)
    db.leads.find_one = AsyncMock(return_value=None)
    db.messages.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    db.leads.update_one = AsyncMock()
    db.campaign_recipients.find_one = AsyncMock(return_value=None)

    lead_doc = {
        "_id": lead_id,
        "user_id": str(user_id),
        "phone": "+447700900123",
        "whatsapp_consent_status": "unknown",
    }

    with (
        patch.object(wh, "get_db", return_value=db),
        patch("app.security.rate_limit.rate_limit_webhook"),
        patch.object(wh.twilio_service, "validate_signature", return_value=True),
        patch("app.services.inbound_whatsapp.lead_service.find_or_create_by_phone", new=AsyncMock(return_value=lead_doc)),
        patch("app.services.inbound_whatsapp.lead_service.get_lead", new=AsyncMock(return_value=lead_doc)),
        patch("app.services.inbound_whatsapp.lead_service.ai_suppressed", return_value=True),
        patch("app.services.inbound_whatsapp.recalculate_lead_score", new=AsyncMock()),
        patch("app.services.inbound_whatsapp.ws_manager.push", new=AsyncMock()),
        patch.object(wh, "enqueue") as enq,
    ):
        resp = await wh.whatsapp_webhook(_FakeRequest(form_data))

    assert resp.status_code == 200
    from app.workers import tasks as tasks_mod

    welcome_calls = [c for c in enq.call_args_list if c.args and c.args[0] is tasks_mod.send_welcome_and_terms]
    assert len(welcome_calls) == 1
    assert welcome_calls[0].kwargs.get("queue") == "high"


# ---------------------------------------------------------------------------
# C12/C13: AI truncated / empty response handling
# ---------------------------------------------------------------------------


class _FakeChoice:
    def __init__(self, content, finish_reason):
        self.message = SimpleNamespace(content=content)
        self.finish_reason = finish_reason


class _FakeResp:
    def __init__(self, content, finish_reason, in_tok=10, out_tok=5):
        self.choices = [_FakeChoice(content, finish_reason)]
        self.usage = SimpleNamespace(prompt_tokens=in_tok, completion_tokens=out_tok)


def test_chat_completion_retries_and_succeeds_after_length_finish(monkeypatch):
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider.settings, "OPENAI_API_KEY", "sk-test")
    responses = [_FakeResp("cut off...", "length"), _FakeResp("full final answer", "stop")]
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(side_effect=responses)

    with patch.object(ai_provider, "_client_get", return_value=fake_client):
        result = ai_provider.chat_completion(
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            model="gpt-4o-mini",
            max_tokens=100,
        )

    assert result.success is True
    assert result.text == "full final answer"
    assert result.finish_reason == "stop"
    assert fake_client.chat.completions.create.call_count == 2
    second_kwargs = fake_client.chat.completions.create.call_args_list[1].kwargs
    assert second_kwargs["max_tokens"] > 100


def test_chat_completion_returns_truncated_response_when_still_length(monkeypatch):
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider.settings, "OPENAI_API_KEY", "sk-test")
    responses = [_FakeResp("cut off", "length"), _FakeResp("still cut off", "length")]
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(side_effect=responses)

    with patch.object(ai_provider, "_client_get", return_value=fake_client):
        result = ai_provider.chat_completion(messages=[{"role": "user", "content": "hi"}], model="gpt-4o-mini")

    assert result.success is False
    assert result.error_category == "truncated_response"
    assert fake_client.chat.completions.create.call_count == 2


def test_chat_completion_returns_empty_response_category(monkeypatch):
    from app.services import ai_provider

    monkeypatch.setattr(ai_provider.settings, "OPENAI_API_KEY", "sk-test")
    fake_client = MagicMock()
    fake_client.chat.completions.create = MagicMock(return_value=_FakeResp("   ", "stop"))

    with patch.object(ai_provider, "_client_get", return_value=fake_client):
        result = ai_provider.chat_completion(messages=[{"role": "user", "content": "hi"}], model="gpt-4o-mini")

    assert result.success is False
    assert result.error_category == "empty_response"
    assert fake_client.chat.completions.create.call_count == 1


def test_generate_reply_raises_runtime_error_with_empty_response_category():
    from app.services import openai_service
    from app.services.ai_provider import AIResult

    ai_settings = {
        "model": "gpt-4o-mini",
        "fallback_model": None,
        "temperature": 0.5,
        "max_output_tokens": 300,
        "default_language": "en",
        "enabled": True,
    }
    with patch.object(
        openai_service, "chat_completion", return_value=AIResult(success=False, error_category="empty_response")
    ):
        with pytest.raises(RuntimeError, match="empty_response"):
            openai_service.generate_reply(None, [], "Acme", ai_settings=ai_settings)


def test_generate_reply_raises_runtime_error_with_truncated_response_category():
    from app.services import openai_service
    from app.services.ai_provider import AIResult

    ai_settings = {
        "model": "gpt-4o-mini",
        "fallback_model": None,
        "temperature": 0.5,
        "max_output_tokens": 300,
        "default_language": "en",
        "enabled": True,
    }
    with patch.object(
        openai_service,
        "chat_completion",
        return_value=AIResult(success=False, error_category="truncated_response"),
    ):
        with pytest.raises(RuntimeError, match="truncated_response"):
            openai_service.generate_reply(None, [], "Acme", ai_settings=ai_settings)


def _ai_task_common_patches(db, *, exc: Exception):
    from app.workers import tasks

    return (
        patch.object(tasks, "_db", return_value=db),
        patch("app.services.ai_config.resolve_ai_settings", return_value={"enabled": True, "moderation_enabled": False}),
        patch.object(
            tasks,
            "get_whatsapp_send_eligibility",
            return_value=SimpleNamespace(allowed=True, reason_code="ok", safe_message="", consent_status="opted_in", window_status="open"),
        ),
        patch("app.services.ai_quota.check_quota", return_value=(True, None)),
        patch.object(tasks, "claim_idempotency", return_value=True),
        patch("app.services.ai_summary.get_summary", return_value=None),
        patch("app.services.ai_summary.maybe_enqueue_summary"),
        patch("app.services.ai_context.load_conversation_context", return_value={"messages": [], "summary_used": False}),
        patch("app.services.notifications.create_notification_sync"),
        patch.object(tasks.openai_service, "generate_reply", side_effect=exc),
    )


@pytest.mark.parametrize("category", ["empty_response", "truncated_response"])
def test_generate_and_send_ai_reply_marks_needs_human_for_hard_ai_failures(monkeypatch, category):
    from app.workers import tasks

    # Prove the override fires even when the generic toggle is off.
    monkeypatch.setattr(tasks.settings, "AI_FAILURE_MARK_NEEDS_HUMAN", False)
    monkeypatch.setattr(tasks.settings, "AI_FAILURE_FALLBACK_ENABLED", False)

    user_id = str(ObjectId())
    lead_id = str(ObjectId())
    lead = {"_id": ObjectId(lead_id), "user_id": user_id, "phone": "+447700900000"}

    db = MagicMock()
    db.leads.find_one = MagicMock(return_value=lead)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id)})
    db.agents.find_one = MagicMock(return_value=None)
    db.messages.find_one = MagicMock(return_value=None)

    patches = _ai_task_common_patches(db, exc=RuntimeError(category))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
        tasks.generate_and_send_ai_reply(user_id, lead_id)

    needs_human_calls = [
        c
        for c in db.leads.update_one.call_args_list
        if c[0][1].get("$set", {}).get("needs_human") is True
    ]
    assert needs_human_calls, "expected needs_human to be set True"
    error_cat_calls = [
        c
        for c in db.leads.update_one.call_args_list
        if c[0][1].get("$set", {}).get("last_ai_error_category") == category
    ]
    assert error_cat_calls
    db.messages.insert_one.assert_not_called()


# ---------------------------------------------------------------------------
# D6: unique (user_id, phone) index + race handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_or_create_by_phone_race_returns_existing():
    from app.services import lead_service

    user_id = str(ObjectId())
    existing_doc = {"_id": ObjectId(), "user_id": user_id, "phone": "+447700900123", "name": "Existing"}

    db = MagicMock()
    db.leads.find_one = AsyncMock(side_effect=[None, existing_doc])
    db.leads.insert_one = AsyncMock(side_effect=DuplicateKeyError("dup key"))

    with patch.object(lead_service, "get_db", return_value=db):
        result = await lead_service.find_or_create_by_phone(user_id, "07700900123")

    assert str(result["_id"]) == str(existing_doc["_id"])


class _FakeAsyncCursor:
    def __init__(self, items):
        self._items = list(items)

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for item in self._items:
            yield item


@pytest.mark.asyncio
async def test_ensure_leads_phone_index_keeps_nonunique_when_duplicates_exist():
    from app.db import mongo as mongo_mod

    db = MagicMock()
    db.leads.aggregate = MagicMock(
        return_value=_FakeAsyncCursor([{"_id": {"user_id": "u1", "phone": "+1"}, "n": 2}])
    )
    db.leads.index_information = AsyncMock(return_value={})
    db.leads.create_index = AsyncMock()
    db.leads.drop_index = AsyncMock()

    await mongo_mod._ensure_leads_phone_index(db)

    db.leads.create_index.assert_awaited_once()
    args, kwargs = db.leads.create_index.call_args
    assert kwargs.get("unique") is False
    db.leads.drop_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_leads_phone_index_enables_unique_when_no_duplicates():
    from app.db import mongo as mongo_mod

    db = MagicMock()
    db.leads.aggregate = MagicMock(return_value=_FakeAsyncCursor([]))
    db.leads.index_information = AsyncMock(return_value={})
    db.leads.create_index = AsyncMock()
    db.leads.drop_index = AsyncMock()

    await mongo_mod._ensure_leads_phone_index(db)

    db.leads.create_index.assert_awaited_once()
    args, kwargs = db.leads.create_index.call_args
    assert kwargs.get("unique") is True


@pytest.mark.asyncio
async def test_ensure_leads_phone_index_drops_and_recreates_when_switching_to_unique():
    from app.db import mongo as mongo_mod

    db = MagicMock()
    db.leads.aggregate = MagicMock(return_value=_FakeAsyncCursor([]))
    db.leads.index_information = AsyncMock(
        return_value={"user_id_1_phone_1": {"key": [("user_id", 1), ("phone", 1)], "unique": False}}
    )
    db.leads.create_index = AsyncMock()
    db.leads.drop_index = AsyncMock()

    await mongo_mod._ensure_leads_phone_index(db)

    db.leads.drop_index.assert_awaited_once_with("user_id_1_phone_1")
    db.leads.create_index.assert_awaited_once()
    assert db.leads.create_index.call_args.kwargs.get("unique") is True


@pytest.mark.asyncio
async def test_ensure_leads_phone_index_noop_when_already_correct():
    from app.db import mongo as mongo_mod

    db = MagicMock()
    db.leads.aggregate = MagicMock(return_value=_FakeAsyncCursor([]))
    db.leads.index_information = AsyncMock(
        return_value={"user_id_1_phone_1": {"key": [("user_id", 1), ("phone", 1)], "unique": True}}
    )
    db.leads.create_index = AsyncMock()
    db.leads.drop_index = AsyncMock()

    await mongo_mod._ensure_leads_phone_index(db)

    db.leads.create_index.assert_not_awaited()
    db.leads.drop_index.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_duplicate_leads_returns_expected_shape():
    from scripts import report_duplicate_leads as script

    fake_db = MagicMock()
    fake_db.leads.aggregate = MagicMock(
        return_value=_FakeAsyncCursor(
            [
                {
                    "_id": {"user_id": "u1", "phone": "+447700900000"},
                    "count": 2,
                    "lead_ids": ["a", "b"],
                    "created_ats": [None, None],
                }
            ]
        )
    )

    with patch("app.db.mongo.get_db", return_value=fake_db):
        groups = await script.find_duplicate_groups()

    assert groups == [{"user_id": "u1", "phone": "+447700900000", "count": 2, "lead_ids": ["a", "b"]}]


# ---------------------------------------------------------------------------
# Live Chat marketing template consent
# ---------------------------------------------------------------------------


def test_message_send_template_does_not_default_purpose():
    from app.models.message import MessageSend

    m = MessageSend(lead_id="x", content_sid="HXabc")
    assert m.message_purpose is None


def test_message_send_non_template_defaults_conversational():
    from app.models.message import MessageSend

    m = MessageSend(lead_id="x", message="hi")
    assert m.message_purpose == "conversational"


def test_message_send_template_with_explicit_purpose_preserved():
    from app.models.message import MessageSend

    m = MessageSend(lead_id="x", content_sid="HXabc", message_purpose="Marketing")
    assert m.message_purpose == "marketing"


@pytest.mark.asyncio
async def test_send_message_route_requires_purpose_for_template():
    from app.routes import messages as msg_routes
    from app.models.message import MessageSend

    user_id = str(ObjectId())
    lead = {"_id": ObjectId(), "phone": "+447700900000"}
    payload = MessageSend(lead_id=str(lead["_id"]), content_sid="HXabc")

    with (
        patch.object(msg_routes.lead_service, "get_lead", new=AsyncMock(return_value=lead)),
        patch("app.security.rate_limit.rate_limit_send"),
        patch("app.routes.messages.resolve_lead_whatsapp_provider", new=AsyncMock(return_value="twilio")),
    ):
        with pytest.raises(HTTPException) as exc:
            await msg_routes.send_message(payload, user={"_id": user_id})

    assert exc.value.status_code == 400
    assert "message_purpose" in exc.value.detail


# ---------------------------------------------------------------------------
# Blast reliability parity
# ---------------------------------------------------------------------------


def test_process_blast_recipient_skips_when_already_claimed():
    from app.workers import tasks

    db = MagicMock()
    db.blast_recipients.find_one_and_update = MagicMock(return_value=None)

    with patch.object(tasks.twilio_service, "send_whatsapp") as send:
        tasks._process_blast_recipient(
            db,
            "u1",
            {"_id": ObjectId(), "phone": "+447700900001"},
            body="hi",
            media_url=None,
            content_sid=None,
            content_variables=None,
            purpose="marketing",
        )
    send.assert_not_called()


def test_process_blast_recipient_retries_on_transient_send_error():
    from app.workers import tasks

    user_id = str(ObjectId())
    recipient = {"_id": ObjectId(), "phone": "+447700900001", "status": "pending", "attempt_count": 0}
    updated_doc = {**recipient, "status": "processing", "attempt_count": 1}

    db = MagicMock()
    db.blast_recipients.find_one_and_update = MagicMock(return_value=updated_doc)
    db.leads.find_one = MagicMock(
        return_value={"phone": "+447700900001", "whatsapp_consent_status": "opted_in", "blacklisted": False}
    )
    db.blacklist.find_one = MagicMock(return_value=None)
    db.blast_recipients.update_one = MagicMock()

    with (
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(tasks, "release_send_permit"),
        patch.object(tasks.twilio_service, "send_whatsapp", side_effect=RuntimeError("connection timeout")),
    ):
        # has_template bypasses the 24h window requirement for marketing purpose.
        tasks._process_blast_recipient(
            db,
            user_id,
            recipient,
            body=None,
            media_url=None,
            content_sid="HXabc",
            content_variables=None,
            purpose="marketing",
        )

    final_set = db.blast_recipients.update_one.call_args[0][1]["$set"]
    assert final_set["status"] == "retrying"
    assert "next_retry_at" in final_set


def test_process_blast_recipient_fails_immediately_on_non_retryable_error():
    from app.workers import tasks

    user_id = str(ObjectId())
    recipient = {"_id": ObjectId(), "phone": "+447700900002", "status": "pending", "attempt_count": 0}
    updated_doc = {**recipient, "status": "processing", "attempt_count": 1}

    db = MagicMock()
    db.blast_recipients.find_one_and_update = MagicMock(return_value=updated_doc)
    db.leads.find_one = MagicMock(
        return_value={"phone": "+447700900002", "whatsapp_consent_status": "opted_in", "blacklisted": False}
    )
    db.blacklist.find_one = MagicMock(return_value=None)
    db.blast_recipients.update_one = MagicMock()

    with (
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(tasks, "release_send_permit"),
        patch.object(
            tasks.twilio_service, "send_whatsapp", side_effect=RuntimeError("Template is not approved")
        ),
    ):
        tasks._process_blast_recipient(
            db,
            user_id,
            recipient,
            body=None,
            media_url=None,
            content_sid="HXabc",
            content_variables=None,
            purpose="marketing",
        )

    final_set = db.blast_recipients.update_one.call_args[0][1]["$set"]
    assert final_set["status"] == "failed"


def test_process_blast_recipient_marks_failed_after_max_retries():
    from app.workers import tasks

    user_id = str(ObjectId())
    recipient = {"_id": ObjectId(), "phone": "+447700900003", "status": "retrying", "attempt_count": 3}
    updated_doc = {**recipient, "status": "processing", "attempt_count": tasks.max_retries() + 1}

    db = MagicMock()
    db.blast_recipients.find_one_and_update = MagicMock(return_value=updated_doc)
    db.leads.find_one = MagicMock(
        return_value={"phone": "+447700900003", "whatsapp_consent_status": "opted_in", "blacklisted": False}
    )
    db.blacklist.find_one = MagicMock(return_value=None)
    db.blast_recipients.update_one = MagicMock()

    with (
        patch.object(tasks, "acquire_send_permit", return_value=True),
        patch.object(tasks, "release_send_permit"),
        patch.object(tasks.twilio_service, "send_whatsapp", side_effect=RuntimeError("connection timeout")),
    ):
        tasks._process_blast_recipient(
            db,
            user_id,
            recipient,
            body=None,
            media_url=None,
            content_sid="HXabc",
            content_variables=None,
            purpose="marketing",
        )

    final_set = db.blast_recipients.update_one.call_args[0][1]["$set"]
    assert final_set["status"] == "failed"


def _finalize_db(agg_rows, *, status="sending", total=None):
    # find_one always returns the same fixed dict (a MagicMock can't apply the
    # $set from update_one like real Mongo would), so seed it with the counts
    # that _recount_blast's aggregate() would have produced.
    counts = {str(row["_id"]): int(row["n"]) for row in agg_rows}
    db = MagicMock()
    db.blast_recipients.aggregate = MagicMock(return_value=agg_rows)
    blast_doc = {
        "_id": ObjectId(),
        "status": status,
        "total_recipients": total,
        "sent_count": sum(
            counts.get(s, 0)
            for s in ("sent", "delivered", "read", "queued", "accepted", "sending")
        ),
        "failed_count": counts.get("failed", 0),
        "cancelled_count": counts.get("cancelled", 0),
    }
    db.blast_campaigns.find_one = MagicMock(return_value=blast_doc)
    db.blast_campaigns.update_one = MagicMock()
    return db


def _statuses_set(db):
    return [
        c.args[1]["$set"]["status"]
        for c in db.blast_campaigns.update_one.call_args_list
        if "status" in c.args[1]["$set"]
    ]


def test_finalize_blast_partially_completed_when_some_failed():
    from app.workers import tasks

    db = _finalize_db([{"_id": "sent", "n": 3}, {"_id": "failed", "n": 2}], total=5)
    with patch.object(tasks, "_publish"):
        tasks._finalize_blast(db, "u1", str(ObjectId()))
    assert "partially_completed" in _statuses_set(db)


def test_finalize_blast_failed_when_all_failed():
    from app.workers import tasks

    db = _finalize_db([{"_id": "failed", "n": 5}], total=5)
    with patch.object(tasks, "_publish"):
        tasks._finalize_blast(db, "u1", str(ObjectId()))
    assert "failed" in _statuses_set(db)


def test_finalize_blast_completed_when_all_sent():
    from app.workers import tasks

    db = _finalize_db([{"_id": "sent", "n": 5}], total=5)
    with patch.object(tasks, "_publish"):
        tasks._finalize_blast(db, "u1", str(ObjectId()))
    assert "completed" in _statuses_set(db)


def test_finalize_blast_completed_when_twilio_queued():
    """Twilio create returns status=queued; that must count as sent, not 0/N."""
    from app.workers import tasks

    db = _finalize_db([{"_id": "queued", "n": 2}], total=2)
    with patch.object(tasks, "_publish"):
        tasks._finalize_blast(db, "u1", str(ObjectId()))
    assert "completed" in _statuses_set(db)
    # recount must persist sent_count > 0
    set_calls = [
        c.args[1]["$set"]
        for c in db.blast_campaigns.update_one.call_args_list
        if "sent_count" in c.args[1].get("$set", {})
    ]
    assert set_calls
    assert set_calls[0]["sent_count"] == 2


def test_finalize_blast_noop_when_already_terminal():
    from app.workers import tasks

    db = _finalize_db([], status="cancelled", total=5)
    with patch.object(tasks, "_publish"):
        tasks._finalize_blast(db, "u1", str(ObjectId()))
    assert _statuses_set(db) == []


def test_send_blast_messages_noop_when_paused():
    from app.workers import tasks

    user_id = str(ObjectId())
    blast_id = str(ObjectId())
    db = MagicMock()
    db.blast_campaigns.find_one = MagicMock(
        return_value={"_id": ObjectId(blast_id), "user_id": user_id, "status": "paused"}
    )
    with patch.object(tasks, "_db", return_value=db):
        tasks.send_blast_messages(user_id, blast_id)
    db.blast_recipients.find.assert_not_called()


def test_send_blast_messages_cancels_open_recipients_when_cancelled():
    from app.workers import tasks

    user_id = str(ObjectId())
    blast_id = str(ObjectId())
    db = MagicMock()
    db.blast_campaigns.find_one = MagicMock(
        return_value={"_id": ObjectId(blast_id), "user_id": user_id, "status": "cancelled"}
    )
    db.blast_recipients.aggregate = MagicMock(return_value=[])
    with patch.object(tasks, "_db", return_value=db), patch.object(tasks, "_publish"):
        tasks.send_blast_messages(user_id, blast_id)
    db.blast_recipients.update_many.assert_called_once()
    assert db.blast_recipients.update_many.call_args[0][1]["$set"]["status"] == "cancelled"


def test_send_blast_messages_chains_next_batch_when_recipients_remain():
    from app.workers import tasks

    user_id = str(ObjectId())
    blast_id = str(ObjectId())
    blast_doc = {
        "_id": ObjectId(blast_id),
        "user_id": user_id,
        "status": "sending",
        "content_sid": None,
        "message": "hi",
        "media_url": None,
        "message_purpose": "marketing",
    }
    recipient = {"_id": ObjectId(), "phone": "+447700900010", "status": "pending"}

    db = MagicMock()
    db.blast_campaigns.find_one = MagicMock(return_value=blast_doc)
    find_mock = MagicMock()
    find_mock.sort.return_value.limit.return_value = [recipient]
    db.blast_recipients.find = MagicMock(return_value=find_mock)
    db.blast_recipients.aggregate = MagicMock(return_value=[{"_id": "pending", "n": 1}])
    db.blast_recipients.count_documents = MagicMock(return_value=1)

    fake_queue = MagicMock()
    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "_process_blast_recipient") as proc,
        patch.object(tasks, "_publish"),
        patch("app.workers.queue.get_queue", return_value=fake_queue),
    ):
        tasks.send_blast_messages(user_id, blast_id)

    proc.assert_called_once()
    fake_queue.enqueue_in.assert_called_once()


def test_send_blast_messages_finalizes_when_no_batch_and_none_open():
    from app.workers import tasks

    user_id = str(ObjectId())
    blast_id = str(ObjectId())
    blast_doc = {
        "_id": ObjectId(blast_id),
        "user_id": user_id,
        "status": "sending",
        "content_sid": None,
        "message": "hi",
        "media_url": None,
        "message_purpose": "marketing",
        "total_recipients": 2,
    }
    db = MagicMock()
    db.blast_campaigns.find_one = MagicMock(return_value=blast_doc)
    find_mock = MagicMock()
    find_mock.sort.return_value.limit.return_value = []
    db.blast_recipients.find = MagicMock(return_value=find_mock)
    db.blast_recipients.count_documents = MagicMock(return_value=0)
    db.blast_recipients.aggregate = MagicMock(return_value=[{"_id": "sent", "n": 2}])

    with (
        patch.object(tasks, "_db", return_value=db),
        patch.object(tasks, "_publish"),
    ):
        tasks.send_blast_messages(user_id, blast_id)

    assert "completed" in _statuses_set(db)


@pytest.mark.asyncio
async def test_pause_blast_route_only_allows_sending_status():
    from app.routes import campaigns as camp_routes

    user_id = ObjectId()
    bid = ObjectId()
    db = MagicMock()
    db.blast_campaigns.find_one = AsyncMock(
        return_value={"_id": bid, "user_id": str(user_id), "status": "queued"}
    )
    with patch.object(camp_routes, "get_db", return_value=db):
        with pytest.raises(HTTPException) as exc:
            await camp_routes.pause_blast(str(bid), user={"_id": user_id})
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_cancel_blast_route_cancels_open_recipients():
    from app.routes import campaigns as camp_routes

    user_id = ObjectId()
    bid = ObjectId()
    db = MagicMock()
    db.blast_campaigns.find_one = AsyncMock(
        side_effect=[
            {"_id": bid, "user_id": str(user_id), "status": "sending"},
            {"_id": bid, "user_id": str(user_id), "status": "cancelled"},
        ]
    )
    db.blast_campaigns.update_one = AsyncMock()
    db.blast_recipients.update_many = AsyncMock()
    with (
        patch.object(camp_routes, "get_db", return_value=db),
        patch.object(camp_routes.ws_manager, "push", new=AsyncMock()),
    ):
        result = await camp_routes.cancel_blast(str(bid), user={"_id": user_id})
    assert result["status"] == "cancelled"
    db.blast_recipients.update_many.assert_awaited_once()
