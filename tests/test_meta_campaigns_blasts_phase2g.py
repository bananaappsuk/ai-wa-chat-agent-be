"""Phase 2G Meta campaigns/blasts — no live Graph or Twilio."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId
from fastapi import HTTPException
from pydantic import ValidationError

from app.models.campaign import BlastCreate, CampaignCreate
from app.routes import campaigns as camp_routes
from app.services.campaign_provider import classify_bulk_send_error, resolve_campaign_template, stored_provider
from app.services.meta_whatsapp_service import MetaWhatsAppError
from app.services.status_callback import apply_meta_status_update
from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility
from app.workers import campaign_tasks as ct
from app.workers import tasks
from tests.test_meta_inbound_phase2a import MemColl, MemDB
from tests.test_meta_templates_phase2f import PNID, TOKEN, WABA, _approved_meta_tmpl


VARS = {"1": "42", "header": "Hi", "button:0": "zz"}


def _opted_lead(user_id: str, phone="+447700900000"):
    return {
        "_id": ObjectId(),
        "user_id": user_id,
        "phone": phone,
        "name": "Lead",
        "whatsapp_consent_status": "opted_in",
        "blacklisted": False,
        "last_inbound_at": datetime.now(timezone.utc),
    }


class _EmptyAsync:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _meta_settings(st):
    st.META_ACCESS_TOKEN = TOKEN
    st.META_WABA_ID = WABA
    st.META_PHONE_NUMBER_ID = PNID
    return st


def _meta_connected():
    from app.services.meta_credentials import MetaTenantCredentials

    return patch(
        "app.services.meta_credentials.get_meta_credentials_for_user",
        return_value=MetaTenantCredentials(access_token=TOKEN, phone_number_id=PNID, waba_id=WABA),
    )


def _create_db(user_id: ObjectId, tmpl=None, camp_oid=None):
    camp_oid = camp_oid or ObjectId()
    db = MagicMock()
    if tmpl is not None:
        db.templates.find_one = AsyncMock(return_value=tmpl)
    db.users.find_one = AsyncMock(return_value={"_id": user_id, "meta_phone_number_id": PNID})
    db.campaigns.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=camp_oid))
    db.campaigns.update_one = AsyncMock()
    db.campaigns.delete_one = AsyncMock()
    db.campaigns.find_one = AsyncMock(
        return_value={"_id": camp_oid, "user_id": str(user_id), "name": "M", "provider": "meta"}
    )
    db.blacklist.find = MagicMock(return_value=_EmptyAsync())
    db.leads.find_one = AsyncMock(return_value=_opted_lead(str(user_id)))
    db.campaign_recipients.insert_many = AsyncMock()
    db.campaign_recipients.aggregate = MagicMock(
        return_value=MagicMock(to_list=AsyncMock(return_value=[{"_id": "pending", "n": 1}]))
    )
    return db, camp_oid


@pytest.mark.asyncio
async def test_meta_campaign_requires_sendable_and_stores_provider():
    user_id = ObjectId()
    tmpl = _approved_meta_tmpl(str(user_id))
    db, _ = _create_db(user_id, tmpl)
    with (
        patch.object(camp_routes, "get_db", return_value=db),
        patch.object(camp_routes.ws_manager, "push", new=AsyncMock()),
        patch("app.routes.templates.get_db", return_value=db),
        patch("app.services.campaign_provider.get_db", return_value=db),
        patch("app.security.rate_limit.rate_limit_campaign"),
        patch("app.services.meta_templates.settings") as st,
        _meta_connected(),
    ):
        _meta_settings(st)
        result = await camp_routes.create_campaign(
            CampaignCreate(
                name="Meta Camp",
                template_id=str(tmpl["_id"]),
                content_variables=VARS,
                recipients=["+447700900000"],
                content_mode="template",
            ),
            user={"_id": user_id, "meta_phone_number_id": PNID},
        )
    inserted = db.campaigns.insert_one.await_args.args[0]
    assert inserted["provider"] == "meta"
    assert inserted["content_sid"] is None
    assert inserted["meta_template_name"] == "order_update"
    assert inserted["meta_language_code"] == "en_US"
    assert result.get("provider") == "meta"


@pytest.mark.asyncio
async def test_campaign_without_template_is_twilio():
    user_id = ObjectId()
    db, _ = _create_db(user_id)
    db.campaigns.find_one = AsyncMock(return_value={"_id": ObjectId(), "provider": "twilio"})
    with (
        patch.object(camp_routes, "get_db", return_value=db),
        patch.object(camp_routes.ws_manager, "push", new=AsyncMock()),
        patch("app.security.rate_limit.rate_limit_campaign"),
    ):
        await camp_routes.create_campaign(
            CampaignCreate(name="T", message="hello", recipients=["+447700900000"]),
            user={"_id": user_id},
        )
    assert db.campaigns.insert_one.await_args.args[0]["provider"] == "twilio"


@pytest.mark.asyncio
async def test_start_meta_without_template_rejected():
    user_id = ObjectId()
    with (
        patch.object(
            camp_routes,
            "_get_owned",
            new=AsyncMock(
                return_value={
                    "status": "draft",
                    "total_recipients": 1,
                    "provider": "meta",
                    "template_id": None,
                }
            ),
        ),
        patch("app.security.rate_limit.rate_limit_campaign"),
        pytest.raises(HTTPException) as exc,
    ):
        await camp_routes.start_campaign(
            str(ObjectId()),
            user={"_id": user_id, "meta_phone_number_id": PNID},
            confirm_marketing=True,
        )
    assert exc.value.status_code == 400
    assert "template" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_meta_ai_campaign_rejected():
    user_id = ObjectId()
    tmpl = _approved_meta_tmpl(str(user_id))
    db, _ = _create_db(user_id, tmpl)
    with (
        patch.object(camp_routes, "get_db", return_value=db),
        patch("app.routes.templates.get_db", return_value=db),
        patch("app.services.campaign_provider.get_db", return_value=db),
        patch("app.security.rate_limit.rate_limit_campaign"),
        pytest.raises(HTTPException) as exc,
    ):
        await camp_routes.create_campaign(
            CampaignCreate(
                name="AI",
                content_mode="ai_agent",
                agent_id=str(ObjectId()),
                campaign_goal="Sell AI Summer Camp Essentials 2026",
                template_id=str(tmpl["_id"]),
                fallback_template_id=str(tmpl["_id"]),
                recipients=["+447700900000"],
            ),
            user={"_id": user_id, "meta_phone_number_id": PNID},
        )
    assert exc.value.status_code == 400
    assert "AI Agent" in str(exc.value.detail)


def test_meta_media_campaign_rejected_at_model_and_resolver():
    with pytest.raises(ValidationError):
        CampaignCreate(
            name="Media",
            template_id=str(ObjectId()),
            media_url="https://example.com/a.jpg",
            recipients=["+447700900000"],
        )


@pytest.mark.asyncio
async def test_resolve_rejects_meta_media():
    user_id = str(ObjectId())
    tmpl = _approved_meta_tmpl(user_id)
    db = MagicMock()
    db.templates.find_one = AsyncMock(return_value=tmpl)
    with patch("app.services.campaign_provider.get_db", return_value=db), pytest.raises(HTTPException) as exc:
        await resolve_campaign_template(
            user_id=user_id,
            template_id=str(tmpl["_id"]),
            media_url="https://example.com/a.jpg",
            content_mode="template",
        )
    assert exc.value.status_code == 400
    assert "Media" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_cross_provider_twilio_helper_rejects_meta_id():
    from app.routes.templates import get_approved_template

    user_id = str(ObjectId())
    tmpl = _approved_meta_tmpl(user_id)
    mem = MemDB()
    mem.templates = MemColl()
    mem.templates.docs.append(tmpl)
    with patch("app.routes.templates.get_db", return_value=mem), pytest.raises(HTTPException) as exc:
        await get_approved_template(user_id, str(tmpl["_id"]))
    assert exc.value.status_code == 400


def test_meta_campaign_consent_required():
    with patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True):
        elig = get_whatsapp_send_eligibility(
            lead={"phone": "+447700900000", "whatsapp_consent_status": "unknown", "blacklisted": False},
            phone="+447700900000",
            purpose="campaign",
            has_template=True,
            provider="meta",
        )
    assert elig.allowed is False
    assert elig.reason_code == "consent_required"


def test_blacklist_blocked():
    with patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True):
        elig = get_whatsapp_send_eligibility(
            lead={"phone": "+447700900000", "whatsapp_consent_status": "opted_in", "blacklisted": True},
            phone="+447700900000",
            purpose="campaign",
            has_template=True,
            provider="meta",
        )
    assert elig.allowed is False
    assert elig.reason_code == "consent_blocked"


def _meta_worker_patches(db, send_side=None, send_return=None):
    kw = {
        "return_value": send_return
        or {"provider": "meta", "provider_message_id": "wamid.C", "status": "sent"}
    }
    if send_side is not None:
        kw = {"side_effect": send_side}
    return (
        patch.object(ct, "_db", return_value=db),
        patch.object(ct, "_publish"),
        patch.object(ct, "_refresh_campaign_counters", return_value={}),
        patch.object(ct, "_maybe_complete_campaign"),
        patch("app.services.idempotency.claim_idempotency", return_value=True),
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch("app.services.whatsapp_outbound.send_whatsapp_template", **kw),
        patch.object(ct.twilio_service, "send_whatsapp"),
        patch("app.config.settings.META_ACCESS_TOKEN", TOKEN),
        patch("app.config.settings.META_PHONE_NUMBER_ID", PNID),
        patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True),
    )


def test_campaign_worker_meta_graph_not_twilio():
    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = ObjectId()
    tmpl = _approved_meta_tmpl(user_id)
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={
            "_id": ObjectId(campaign_id),
            "user_id": user_id,
            "status": "running",
            "provider": "meta",
            "template_id": str(tmpl["_id"]),
            "meta_template_name": "order_update",
            "meta_language_code": "en_US",
            "content_variables": VARS,
            "content_mode": "template",
        }
    )
    recipient = {
        "_id": recipient_id,
        "user_id": user_id,
        "campaign_id": campaign_id,
        "phone": "+447700900000",
        "status": "processing",
        "attempt_count": 1,
        "lead_id": None,
    }
    db.campaign_recipients.find_one_and_update = MagicMock(
        side_effect=[recipient, {**recipient, "status": "sent", "provider_message_id": "wamid.C"}]
    )
    db.leads.find_one = MagicMock(return_value=_opted_lead(user_id))
    db.templates.find_one = MagicMock(return_value=tmpl)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "meta_phone_number_id": PNID})
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    db.campaign_recipients.aggregate = MagicMock(return_value=[])
    patches = _meta_worker_patches(db)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7] as send_tpl, patches[8] as twilio, patches[9], patches[10], patches[11]:
        ct.send_campaign_recipient(user_id, campaign_id, str(recipient_id))
    twilio.assert_not_called()
    send_tpl.assert_called_once()
    assert send_tpl.call_args.kwargs.get("provider") == "meta"
    msg = db.messages.insert_one.call_args.args[0]
    assert msg["provider"] == "meta"
    assert msg["provider_message_id"] == "wamid.C"
    assert msg.get("twilio_sid") is None
    assert msg["message_type"] == "template"
    rec_set = db.campaign_recipients.find_one_and_update.call_args_list[-1][0][1]["$set"]
    assert rec_set["provider"] == "meta"
    assert rec_set["provider_message_id"] == "wamid.C"
    assert rec_set.get("twilio_sid") is None


def test_existing_wamid_prevents_second_campaign_send():
    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = ObjectId()
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={"_id": ObjectId(campaign_id), "user_id": user_id, "status": "running", "provider": "meta"}
    )
    db.campaign_recipients.find_one_and_update = MagicMock(
        return_value={
            "_id": recipient_id,
            "provider_message_id": "wamid.EXISTING",
            "phone": "+447700900000",
            "status": "processing",
        }
    )
    db.campaign_recipients.update_one = MagicMock()
    with (
        patch.object(ct, "_db", return_value=db),
        patch.object(ct, "_refresh_campaign_counters", return_value={}),
        patch.object(ct, "_maybe_complete_campaign"),
        patch.object(ct.twilio_service, "send_whatsapp") as twilio,
        patch("app.services.whatsapp_outbound.send_whatsapp_template") as send_tpl,
    ):
        ct.send_campaign_recipient(user_id, campaign_id, str(recipient_id))
    twilio.assert_not_called()
    send_tpl.assert_not_called()


def test_meta_429_retries_and_4xx_fails_classifier():
    assert classify_bulk_send_error(MetaWhatsAppError("rate", status_code=429), provider="meta") == "provider_rate_limited"
    assert classify_bulk_send_error(MetaWhatsAppError("bad", status_code=400), provider="meta") == "non_retryable"
    assert classify_bulk_send_error(MetaWhatsAppError("oops", status_code=503), provider="meta") == "retryable"
    assert stored_provider({}) == "twilio"
    assert stored_provider({"provider": "meta"}) == "meta"


def test_campaign_worker_meta_429_retries_meta_only():
    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = ObjectId()
    tmpl = _approved_meta_tmpl(user_id)
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={
            "_id": ObjectId(campaign_id),
            "user_id": user_id,
            "status": "running",
            "provider": "meta",
            "template_id": str(tmpl["_id"]),
            "meta_template_name": "order_update",
            "meta_language_code": "en_US",
            "content_variables": VARS,
            "content_mode": "template",
        }
    )
    recipient = {
        "_id": recipient_id,
        "phone": "+447700900000",
        "status": "processing",
        "attempt_count": 1,
        "lead_id": None,
    }
    db.campaign_recipients.find_one_and_update = MagicMock(side_effect=[recipient, {**recipient, "status": "retrying"}])
    db.leads.find_one = MagicMock(return_value=_opted_lead(user_id))
    db.templates.find_one = MagicMock(return_value=tmpl)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "meta_phone_number_id": PNID})
    q = MagicMock()
    patches = list(_meta_worker_patches(db, send_side=MetaWhatsAppError("rate", status_code=429)))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8] as twilio, patches[9], patches[10], patches[11], patch.object(ct, "_queue", return_value=q):
        ct.send_campaign_recipient(user_id, campaign_id, str(recipient_id))
    twilio.assert_not_called()
    rec_set = db.campaign_recipients.find_one_and_update.call_args_list[-1][0][1]["$set"]
    assert rec_set["status"] == "retrying"
    q.enqueue_in.assert_called()


def test_campaign_worker_meta_hard_4xx_fails():
    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = ObjectId()
    tmpl = _approved_meta_tmpl(user_id)
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={
            "_id": ObjectId(campaign_id),
            "user_id": user_id,
            "status": "running",
            "provider": "meta",
            "template_id": str(tmpl["_id"]),
            "meta_template_name": "order_update",
            "meta_language_code": "en_US",
            "content_variables": VARS,
            "content_mode": "template",
        }
    )
    recipient = {
        "_id": recipient_id,
        "phone": "+447700900000",
        "status": "processing",
        "attempt_count": 1,
        "lead_id": None,
    }
    db.campaign_recipients.find_one_and_update = MagicMock(
        side_effect=[recipient, {**recipient, "status": "failed"}]
    )
    db.leads.find_one = MagicMock(return_value=_opted_lead(user_id))
    db.templates.find_one = MagicMock(return_value=tmpl)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "meta_phone_number_id": PNID})
    patches = list(_meta_worker_patches(db, send_side=MetaWhatsAppError("bad template", status_code=400)))
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8] as twilio, patches[9], patches[10], patches[11]:
        ct.send_campaign_recipient(user_id, campaign_id, str(recipient_id))
    twilio.assert_not_called()
    failed = db.campaign_recipients.find_one_and_update.call_args_list[-1][0][1]["$set"]
    assert failed["status"] == "failed"


def test_pause_respected_meta_campaign():
    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={"_id": ObjectId(campaign_id), "user_id": user_id, "status": "paused", "provider": "meta"}
    )
    with patch.object(ct, "_db", return_value=db), patch.object(ct.twilio_service, "send_whatsapp") as twilio:
        ct.send_campaign_recipient(user_id, campaign_id, str(ObjectId()))
    twilio.assert_not_called()


def test_scheduler_still_starts():
    db = MagicMock()
    oid = ObjectId()
    cur = MagicMock()
    cur.limit = MagicMock(
        return_value=[{"_id": oid, "user_id": str(ObjectId()), "status": "scheduled", "content_mode": "template"}]
    )
    db.campaigns.find = MagicMock(return_value=cur)
    db.campaigns.update_one = MagicMock(return_value=SimpleNamespace(modified_count=1))
    q = MagicMock()
    with patch.object(ct, "_db", return_value=db), patch.object(ct, "_queue", return_value=q):
        n = ct.process_due_scheduled_campaigns()
    assert n == 1
    q.enqueue.assert_called()


@pytest.mark.asyncio
async def test_tenant_isolation_and_pnid_mismatch():
    from app.routes.templates import get_sendable_meta_template

    a = str(ObjectId())
    b = str(ObjectId())
    tmpl = _approved_meta_tmpl(a)
    mem = MemDB()
    mem.templates = MemColl()
    mem.templates.docs.append(tmpl)
    mem.users.docs.append(
        {
            "_id": ObjectId(b),
            "meta_phone_number_id": PNID,
            "meta_waba_id": WABA,
            "meta_connection_status": "connected",
        }
    )
    with (
        patch("app.routes.templates.get_db", return_value=mem),
        patch("app.services.meta_templates.settings") as st,
        _meta_connected(),
        pytest.raises(HTTPException) as exc,
    ):
        _meta_settings(st)
        await get_sendable_meta_template(b, str(tmpl["_id"]))
    assert exc.value.status_code == 404

    other_pnid_user = ObjectId()
    mem2 = MemDB()
    mem2.templates = MemColl()
    mem2.templates.docs.append(_approved_meta_tmpl(str(other_pnid_user)))
    mem2.users.docs.append({"_id": other_pnid_user, "meta_phone_number_id": "OTHER_PNID"})
    with (
        patch("app.routes.templates.get_db", return_value=mem2),
        patch("app.services.meta_templates.settings") as st2,
        pytest.raises(HTTPException) as exc2,
    ):
        _meta_settings(st2)
        await get_sendable_meta_template(str(other_pnid_user), str(mem2.templates.docs[0]["_id"]))
    assert exc2.value.status_code == 403


@pytest.mark.asyncio
async def test_meta_blast_requires_template_rejects_freeform_media():
    user_id = ObjectId()
    tmpl = _approved_meta_tmpl(str(user_id))
    db = MagicMock()
    db.templates.find_one = AsyncMock(return_value=tmpl)
    db.users.find_one = AsyncMock(return_value={"_id": user_id, "meta_phone_number_id": PNID})
    with (
        patch.object(camp_routes, "get_db", return_value=db),
        patch("app.routes.templates.get_db", return_value=db),
        patch("app.services.campaign_provider.get_db", return_value=db),
        patch("app.security.rate_limit.rate_limit_campaign"),
        patch("app.services.throughput.assert_bulk_enqueue_allowed"),
        patch("app.services.meta_templates.settings") as st,
        pytest.raises(HTTPException) as exc,
    ):
        _meta_settings(st)
        await camp_routes.create_blast(
            BlastCreate(
                name="B",
                recipients=["+447700900000"],
                template_id=str(tmpl["_id"]),
                media_url="https://ex.com/a.jpg",
                message_purpose="transactional",
            ),
            user={"_id": user_id, "meta_phone_number_id": PNID},
        )
    assert exc.value.status_code == 400

    empty = await resolve_campaign_template(user_id=str(user_id), template_id=None)
    assert empty["provider"] == "twilio"


@pytest.mark.asyncio
async def test_meta_blast_stores_provider():
    user_id = ObjectId()
    tmpl = _approved_meta_tmpl(str(user_id))
    blast_oid = ObjectId()
    db = MagicMock()
    db.templates.find_one = AsyncMock(return_value=tmpl)
    db.users.find_one = AsyncMock(return_value={"_id": user_id, "meta_phone_number_id": PNID})
    db.blast_campaigns.insert_one = AsyncMock(return_value=SimpleNamespace(inserted_id=blast_oid))
    db.blast_campaigns.update_one = AsyncMock()
    db.blast_campaigns.delete_one = AsyncMock()
    db.blacklist.find = MagicMock(return_value=_EmptyAsync())
    db.leads.find_one = AsyncMock(return_value=_opted_lead(str(user_id)))
    db.blast_recipients.insert_many = AsyncMock()
    db.blast_campaigns.find_one = AsyncMock(
        return_value={"_id": blast_oid, "provider": "meta", "name": "Meta Blast"}
    )
    with (
        patch.object(camp_routes, "get_db", return_value=db),
        patch("app.routes.templates.get_db", return_value=db),
        patch("app.services.campaign_provider.get_db", return_value=db),
        patch("app.security.rate_limit.rate_limit_campaign"),
        patch("app.services.throughput.assert_bulk_enqueue_allowed"),
        patch.object(camp_routes, "enqueue"),
        patch("app.services.meta_templates.settings") as st,
        patch("app.config.settings.META_ACCESS_TOKEN", TOKEN),
        patch("app.config.settings.META_PHONE_NUMBER_ID", PNID),
        _meta_connected(),
    ):
        _meta_settings(st)
        await camp_routes.create_blast(
            BlastCreate(
                name="Meta Blast",
                recipients=["+447700900000"],
                template_id=str(tmpl["_id"]),
                content_variables=VARS,
                message_purpose="transactional",
            ),
            user={"_id": user_id, "meta_phone_number_id": PNID},
        )
    inserted = db.blast_campaigns.insert_one.await_args.args[0]
    assert inserted["provider"] == "meta"
    assert inserted["content_sid"] is None
    rec = db.blast_recipients.insert_many.await_args.args[0][0]
    assert rec["provider"] == "meta"


def test_blast_worker_meta_not_twilio_and_wamid():
    user_id = str(ObjectId())
    tmpl = _approved_meta_tmpl(user_id)
    rid = ObjectId()
    db = MagicMock()
    recipient = {"_id": rid, "phone": "+447700900000", "status": "pending", "attempt_count": 0}
    updated = {**recipient, "status": "processing", "attempt_count": 1}
    db.blast_recipients.find_one_and_update = MagicMock(return_value=updated)
    db.leads.find_one = MagicMock(return_value=_opted_lead(user_id))
    db.blacklist.find_one = MagicMock(return_value=None)
    db.templates.find_one = MagicMock(return_value=tmpl)
    db.users.find_one = MagicMock(return_value={"_id": ObjectId(user_id), "meta_phone_number_id": PNID})
    db.blast_recipients.update_one = MagicMock()
    blast = {
        "provider": "meta",
        "template_id": str(tmpl["_id"]),
        "meta_template_name": "order_update",
        "meta_language_code": "en_US",
        "content_variables": VARS,
    }
    with (
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch(
            "app.workers.tasks.send_whatsapp_template",
            return_value={"provider": "meta", "provider_message_id": "wamid.B"},
        ) as send_tpl,
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "send_whatsapp_text") as send_text,
        patch("app.config.settings.META_ACCESS_TOKEN", TOKEN),
        patch("app.config.settings.META_PHONE_NUMBER_ID", PNID),
        patch("app.services.whatsapp_eligibility._meta_sender_ok", return_value=True),
    ):
        tasks._process_blast_recipient(
            db,
            user_id,
            recipient,
            blast=blast,
            body=None,
            media_url=None,
            content_sid=None,
            content_variables=VARS,
            purpose="transactional",
        )
    twilio.assert_not_called()
    send_text.assert_not_called()
    send_tpl.assert_called_once()
    fields = db.blast_recipients.update_one.call_args[0][1]["$set"]
    assert fields["provider"] == "meta"
    assert fields["provider_message_id"] == "wamid.B"
    assert fields.get("twilio_sid") is None


def test_blast_existing_wamid_skips_send():
    db = MagicMock()
    rid = ObjectId()
    updated = {
        "_id": rid,
        "phone": "+447700900000",
        "provider_message_id": "wamid.X",
        "status": "processing",
    }
    db.blast_recipients.find_one_and_update = MagicMock(return_value=updated)
    db.blast_recipients.update_one = MagicMock()
    with (
        patch.object(tasks.twilio_service, "send_whatsapp") as twilio,
        patch.object(tasks, "send_whatsapp_template") as send_tpl,
    ):
        tasks._process_blast_recipient(
            db,
            str(ObjectId()),
            {"_id": rid, "status": "pending"},
            blast={"provider": "meta"},
            body="hi",
            media_url=None,
            content_sid=None,
            content_variables=None,
            purpose="conversational",
        )
    twilio.assert_not_called()
    send_tpl.assert_not_called()


def test_twilio_freeform_blast_still_calls_twilio():
    db = MagicMock()
    rid = ObjectId()
    updated = {"_id": rid, "phone": "+447700900000", "status": "processing", "attempt_count": 1}
    db.blast_recipients.find_one_and_update = MagicMock(return_value=updated)
    db.leads.find_one = MagicMock(return_value=_opted_lead("u", "+447700900000"))
    db.blacklist.find_one = MagicMock(return_value=None)
    db.blast_recipients.update_one = MagicMock()
    with (
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch.object(tasks.twilio_service, "send_whatsapp", return_value={"sid": "SM1", "status": "queued"}) as twilio,
        patch.object(tasks, "send_whatsapp_template") as send_tpl,
    ):
        tasks._process_blast_recipient(
            db,
            str(ObjectId()),
            {"_id": rid, "status": "pending"},
            blast={"provider": "twilio", "message": "hi"},
            body="hello",
            media_url=None,
            content_sid=None,
            content_variables=None,
            purpose="conversational",
        )
    twilio.assert_called_once()
    send_tpl.assert_not_called()


def test_twilio_hx_blast_still_calls_twilio():
    db = MagicMock()
    rid = ObjectId()
    updated = {"_id": rid, "phone": "+447700900000", "status": "processing", "attempt_count": 1}
    db.blast_recipients.find_one_and_update = MagicMock(return_value=updated)
    db.leads.find_one = MagicMock(return_value=_opted_lead("u"))
    db.blacklist.find_one = MagicMock(return_value=None)
    db.blast_recipients.update_one = MagicMock()
    with (
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch.object(tasks.twilio_service, "send_whatsapp", return_value={"sid": "SMHX", "status": "queued"}) as twilio,
        patch.object(tasks, "send_whatsapp_template") as send_tpl,
    ):
        tasks._process_blast_recipient(
            db,
            str(ObjectId()),
            {"_id": rid, "status": "pending"},
            blast={"provider": "twilio", "content_sid": "HXabc"},
            body=None,
            media_url=None,
            content_sid="HXabc",
            content_variables={"1": "A"},
            purpose="transactional",
        )
    twilio.assert_called_once()
    assert twilio.call_args.kwargs.get("content_sid") == "HXabc" or "HXabc" in str(twilio.call_args)
    send_tpl.assert_not_called()


def test_blast_pause_respected():
    bid = ObjectId()
    db = MagicMock()
    db.blast_campaigns.find_one = MagicMock(
        return_value={"_id": bid, "user_id": "u1", "status": "paused", "provider": "meta"}
    )
    with patch.object(tasks, "_process_blast_recipient") as proc:
        tasks.send_blast_messages("u1", str(bid))
    proc.assert_not_called()


def test_twilio_hx_campaign_worker_still_twilio():
    user_id = str(ObjectId())
    campaign_id = str(ObjectId())
    recipient_id = ObjectId()
    tmpl_id = ObjectId()
    db = MagicMock()
    db.campaigns.find_one = MagicMock(
        return_value={
            "_id": ObjectId(campaign_id),
            "user_id": user_id,
            "status": "running",
            "provider": "twilio",
            "template_id": str(tmpl_id),
            "content_sid": "HXabc",
            "content_variables": {"1": "A"},
        }
    )
    recipient = {
        "_id": recipient_id,
        "phone": "+447700900000",
        "status": "processing",
        "attempt_count": 1,
        "lead_id": None,
    }
    db.campaign_recipients.find_one_and_update = MagicMock(
        side_effect=[recipient, {**recipient, "status": "sent"}]
    )
    db.leads.find_one = MagicMock(return_value=_opted_lead(user_id))
    db.templates.find_one = MagicMock(
        return_value={"_id": tmpl_id, "user_id": user_id, "status": "approved", "content_sid": "HXabc", "name": "W"}
    )
    db.messages.insert_one = MagicMock(return_value=SimpleNamespace(inserted_id=ObjectId()))
    with (
        patch.object(ct, "_db", return_value=db),
        patch.object(ct, "_publish"),
        patch.object(ct, "_refresh_campaign_counters", return_value={}),
        patch.object(ct, "_maybe_complete_campaign"),
        patch("app.services.idempotency.claim_idempotency", return_value=True),
        patch("app.services.throughput.acquire_send_permit", return_value=True),
        patch("app.services.throughput.release_send_permit"),
        patch.object(ct.twilio_service, "send_whatsapp", return_value={"sid": "SM9", "status": "queued"}) as twilio,
        patch("app.services.whatsapp_outbound.send_whatsapp_template") as send_tpl,
        patch.object(
            ct.twilio_service,
            "assert_whatsapp_template_approved_for_out_of_session",
            return_value={"whatsapp_status": "approved"},
        ),
        patch.object(ct.twilio_service, "log_template_send_gate"),
    ):
        ct.send_campaign_recipient(user_id, campaign_id, str(recipient_id))
    twilio.assert_called()
    send_tpl.assert_not_called()


@pytest.mark.asyncio
async def test_meta_status_updates_campaign_and_blast_recipients():
    from app.services import status_callback

    mem = MemDB()
    mem.blast_campaigns = MemColl()
    mem.blast_recipients = MemColl()
    uid = str(ObjectId())
    mem.users.docs.append({"_id": ObjectId(uid), "meta_phone_number_id": PNID})
    cid = str(ObjectId())
    crid = ObjectId()
    mem.campaign_recipients.docs.append(
        {
            "_id": crid,
            "user_id": uid,
            "campaign_id": cid,
            "provider": "meta",
            "provider_message_id": "wamid.CAMP",
            "status": "sent",
        }
    )
    mem.campaigns.docs.append({"_id": ObjectId(cid), "user_id": uid, "status": "running"})
    mem.campaign_recipients.docs.append(
        {
            "_id": ObjectId(),
            "user_id": uid,
            "campaign_id": cid,
            "provider": "meta",
            "provider_message_id": "wamid.FAIL",
            "status": "sent",
        }
    )
    brid = ObjectId()
    bid = str(ObjectId())
    mem.blast_recipients.docs.append(
        {
            "_id": brid,
            "user_id": uid,
            "blast_id": bid,
            "provider": "meta",
            "provider_message_id": "wamid.BL",
            "status": "sent",
        }
    )
    mem.blast_campaigns.docs.append(
        {"_id": ObjectId(bid), "user_id": uid, "status": "sending", "sent_count": 1, "total_recipients": 1}
    )
    with (
        patch.object(status_callback, "_publish_best_effort"),
        patch("app.config.settings.META_PHONE_NUMBER_ID", PNID),
    ):
        r1 = await apply_meta_status_update(
            provider_message_id="wamid.CAMP",
            status_raw="delivered",
            errors=[],
            phone_number_id=PNID,
            db=mem,
        )
        r2 = await apply_meta_status_update(
            provider_message_id="wamid.BL",
            status_raw="read",
            errors=[],
            phone_number_id=PNID,
            db=mem,
        )
        r_fail = await apply_meta_status_update(
            provider_message_id="wamid.FAIL",
            status_raw="failed",
            errors=[{"code": 131026, "title": "undeliverable"}],
            phone_number_id=PNID,
            db=mem,
        )
        r3 = await apply_meta_status_update(
            provider_message_id="wamid.UNKNOWN",
            status_raw="delivered",
            errors=[],
            phone_number_id=PNID,
            db=mem,
        )
        r4 = await apply_meta_status_update(
            provider_message_id="wamid.CAMP",
            status_raw="sent",
            errors=[],
            phone_number_id=PNID,
            db=mem,
        )
    assert r1["updated"] is True
    camp_by_wamid = {d["provider_message_id"]: d for d in mem.campaign_recipients.docs}
    assert camp_by_wamid["wamid.CAMP"]["status"] == "delivered"
    assert r2["updated"] is True
    assert mem.blast_recipients.docs[0]["status"] == "read"
    assert r_fail["updated"] is True
    assert camp_by_wamid["wamid.FAIL"]["status"] == "failed"
    assert r3["updated"] is False
    assert r3["reason"] == "unknown"
    assert r4["updated"] is False
    assert mem.blast_campaigns.docs[0].get("read_count") == 1
