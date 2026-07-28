"""RQ jobs for campaign sending engine."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId
from pymongo import MongoClient, ReturnDocument
from redis import Redis

from app.config import settings
from app.models.campaign import TERMINAL_STATUSES
from app.services import twilio_service
from app.services.campaign_service import (
    RECIPIENT_OPEN,
    finalize_status_from_counts,
    progress_percentage,
    recount_ai_generation_fields,
    recount_campaign_fields,
)
from app.services.whatsapp_window import WINDOW_CLOSED_ERROR, is_whatsapp_window_open
from app.services.phone_norm import normalize_e164

_mongo: MongoClient | None = None
_redis: Redis | None = None


def _db():
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(settings.MONGO_URI)
    return _mongo[settings.MONGO_DB]


def _redis_client() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            settings.REDIS_URL,
            health_check_interval=30,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
    return _redis


def _queue(name: str | None = None):
    from app.workers.queue import get_queue

    return get_queue(name or settings.RQ_BULK_QUEUE_NAME)


def _publish(user_id: str, event: str, data: dict) -> None:
    payload = json.dumps({"event": event, "data": data, "user_id": user_id})
    _redis_client().publish("ws:events", payload)


def _serialize(doc: dict) -> dict:
    out = {}
    for k, v in doc.items():
        if k == "_id":
            out["id"] = str(v)
        elif isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _norm_phone(phone: Optional[str]) -> Optional[str]:
    """Backward-compatible alias for canonical E.164 normalisation."""
    return normalize_e164(phone)


def _resolve_media_url(media_url: Optional[str]) -> Optional[str]:
    if not media_url:
        return None
    url = media_url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("PUBLIC_BASE_URL is required to send media")
    if not url.startswith("/"):
        url = "/" + url
    return f"{base}{url}"


def _status_counts(db, campaign_id: str) -> dict[str, int]:
    pipe = [
        {"$match": {"campaign_id": campaign_id}},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]
    counts: dict[str, int] = {}
    for row in db.campaign_recipients.aggregate(pipe):
        counts[str(row["_id"])] = int(row["n"])
    return counts


def _refresh_campaign_counters(db, user_id: str, campaign_id: str, *, event: str = "campaign:updated") -> dict:
    campaign = db.campaigns.find_one({"_id": ObjectId(campaign_id), "user_id": user_id})
    if not campaign:
        return {}
    counts = _status_counts(db, campaign_id)
    total = sum(counts.values())
    fields = recount_campaign_fields(counts, total)
    # AI generation counters (skipped_closed_window uses ai_generation_status=skipped — not failed)
    ai_counts: dict[str, int] = {}
    for row in db.campaign_recipients.aggregate(
        [
            {"$match": {"campaign_id": campaign_id}},
            {"$group": {"_id": "$ai_generation_status", "n": {"$sum": 1}}},
        ]
    ):
        ai_counts[str(row["_id"] or "")] = int(row["n"])
    fields.update(recount_ai_generation_fields(ai_counts))
    fields["updated_at"] = _utcnow()
    db.campaigns.update_one({"_id": ObjectId(campaign_id)}, {"$set": fields})
    fresh = {**campaign, **fields}
    _publish(user_id, event, _serialize(fresh))
    return fresh


def _maybe_complete_campaign(db, user_id: str, campaign_id: str) -> None:
    campaign = db.campaigns.find_one({"_id": ObjectId(campaign_id), "user_id": user_id})
    if not campaign:
        return
    if campaign.get("status") in TERMINAL_STATUSES | {"paused"}:
        # paused: don't complete; cancelled already terminal
        if campaign.get("status") == "paused":
            return
        if campaign.get("status") in TERMINAL_STATUSES:
            return
    open_n = db.campaign_recipients.count_documents(
        {"campaign_id": campaign_id, "status": {"$in": list(RECIPIENT_OPEN)}}
    )
    if open_n > 0:
        return
    fresh = _refresh_campaign_counters(db, user_id, campaign_id)
    final = finalize_status_from_counts(fresh)
    db.campaigns.update_one(
        {"_id": ObjectId(campaign_id)},
        {"$set": {"status": final, "completed_at": _utcnow(), "updated_at": _utcnow()}},
    )
    done = db.campaigns.find_one({"_id": ObjectId(campaign_id)})
    if done:
        _publish(user_id, "campaign:completed", _serialize(done))
        try:
            from app.services.notifications import create_notification_sync

            status = done.get("status") or final
            ntype = "campaign_failed" if status == "failed" else "campaign_completed"
            create_notification_sync(
                db,
                user_id=user_id,
                type=ntype,
                title="Campaign finished" if ntype == "campaign_completed" else "Campaign failed",
                message=f"Campaign ended with status: {status}.",
                resource_type="campaign",
                resource_id=campaign_id,
                dedupe_key=f"campaign_done:{campaign_id}:{status}",
            )
        except Exception:
            pass


def start_campaign_job(user_id: str, campaign_id: str) -> None:
    db = _db()
    campaign = db.campaigns.find_one({"_id": ObjectId(campaign_id), "user_id": user_id})
    if not campaign:
        return
    if campaign.get("status") not in ("queued", "running", "draft", "scheduled"):
        # resume path may set running before enqueue
        if campaign.get("status") != "running":
            return

    now = _utcnow()
    db.campaigns.update_one(
        {"_id": ObjectId(campaign_id)},
        {
            "$set": {
                "status": "running",
                "started_at": campaign.get("started_at") or now,
                "updated_at": now,
                "last_error": None,
            }
        },
    )
    # Move pending → queued
    db.campaign_recipients.update_many(
        {"campaign_id": campaign_id, "status": "pending"},
        {"$set": {"status": "queued", "updated_at": now}},
    )
    _publish(user_id, "campaign:started", _serialize(db.campaigns.find_one({"_id": ObjectId(campaign_id)})))
    _refresh_campaign_counters(db, user_id, campaign_id)
    process_campaign_batch(user_id, campaign_id)


def process_campaign_batch(user_id: str, campaign_id: str) -> None:
    db = _db()
    campaign = db.campaigns.find_one({"_id": ObjectId(campaign_id), "user_id": user_id})
    if not campaign:
        return
    if campaign.get("status") != "running":
        return

    batch_size = max(1, int(getattr(settings, "CAMPAIGN_BATCH_SIZE", 25)))
    delay_ms = max(0, int(getattr(settings, "CAMPAIGN_SEND_DELAY_MS", 200)))
    now = _utcnow()

    cur = (
        db.campaign_recipients.find(
            {
                "campaign_id": campaign_id,
                "user_id": user_id,
                "$or": [
                    {"status": "queued"},
                    {
                        "status": "retrying",
                        "next_retry_at": {"$lte": now},
                    },
                ],
            }
        )
        .sort("created_at", 1)
        .limit(batch_size)
    )
    from app.services.ai_campaign import is_ai_campaign

    ids = []
    for r in cur:
        if is_ai_campaign(campaign):
            # Only send when content ready (or static template path marked ready)
            if r.get("ai_generation_status") not in ("ready",):
                continue
            if (campaign.get("review_mode") or "sample_review") == "full_review" and not r.get("ai_approved"):
                continue
        ids.append(str(r["_id"]))
    if not ids:
        # If AI still generating, wait; else finalize
        if is_ai_campaign(campaign) and campaign.get("ai_generation_status") == "generating":
            _queue().enqueue_in(timedelta(seconds=2), process_campaign_batch, user_id, campaign_id)
            return
        _maybe_complete_campaign(db, user_id, campaign_id)
        return

    q = _queue()
    for i, rid in enumerate(ids):
        if delay_ms > 0 and i > 0:
            q.enqueue_in(
                timedelta(milliseconds=delay_ms * i),
                send_campaign_recipient,
                user_id,
                campaign_id,
                rid,
            )
        else:
            q.enqueue(send_campaign_recipient, user_id, campaign_id, rid)

    # Chain next batch after this batch's last delay + buffer
    remaining = db.campaign_recipients.count_documents(
        {
            "campaign_id": campaign_id,
            "$or": [
                {"status": "queued"},
                {"status": "retrying"},
                {"status": "pending"},
            ],
            "_id": {"$nin": [ObjectId(x) for x in ids]},
        }
    )
    if remaining > 0:
        wait = timedelta(milliseconds=delay_ms * max(len(ids), 1) + 500)
        q.enqueue_in(wait, process_campaign_batch, user_id, campaign_id)
    else:
        # After sends finish, completion checked inside recipient jobs
        q.enqueue_in(timedelta(seconds=2), finalize_campaign_if_idle, user_id, campaign_id)


def send_campaign_recipient(user_id: str, campaign_id: str, recipient_id: str) -> None:
    db = _db()
    if not ObjectId.is_valid(recipient_id):
        return

    campaign = db.campaigns.find_one({"_id": ObjectId(campaign_id), "user_id": user_id})
    if not campaign:
        return
    status = campaign.get("status")
    if status == "paused":
        return
    if status == "cancelled" or status in TERMINAL_STATUSES:
        db.campaign_recipients.update_one(
            {
                "_id": ObjectId(recipient_id),
                "status": {"$in": ["queued", "pending", "retrying", "processing"]},
            },
            {"$set": {"status": "cancelled", "updated_at": _utcnow()}},
        )
        _refresh_campaign_counters(db, user_id, campaign_id)
        return

    recipient = db.campaign_recipients.find_one_and_update(
        {
            "_id": ObjectId(recipient_id),
            "user_id": user_id,
            "campaign_id": campaign_id,
            "status": {"$in": ["queued", "retrying", "pending"]},
        },
        {
            "$set": {
                "status": "processing",
                "last_attempt_at": _utcnow(),
                "updated_at": _utcnow(),
            },
            "$inc": {"attempt_count": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not recipient:
        return

    _publish(user_id, "campaign:recipient_updated", _serialize(recipient))
    phone = recipient.get("phone")

    from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility
    from app.services.throughput import acquire_send_permit, release_send_permit
    from app.services.idempotency import claim_idempotency, make_idempotency_key
    from app.services.twilio_errors import classify_send_error, is_retryable_category
    from app.services.retry_backoff import compute_retry_delay_seconds, max_retries as wa_max_retries

    try:
        idem = make_idempotency_key("campaign", campaign_id, str(recipient["_id"]))
        if not claim_idempotency(user_id, idem):
            return

        lead = None
        if recipient.get("lead_id") and ObjectId.is_valid(str(recipient["lead_id"])):
            lead = db.leads.find_one({"_id": ObjectId(str(recipient["lead_id"])), "user_id": user_id})
        if not lead:
            lead = db.leads.find_one({"user_id": user_id, "phone": _norm_phone(phone)})

        content_sid = campaign.get("content_sid")
        template_id = campaign.get("template_id")
        has_template = bool(content_sid or template_id or campaign.get("fallback_template_content_sid") or campaign.get("fallback_template_id"))

        # AI Agent Campaign: re-select path at send time; never send free-form if window closed
        from app.services.ai_campaign import (
            delivery_scope as resolve_delivery_scope,
            is_ai_campaign,
            resolve_fallback_template,
            select_content_path,
        )

        ai_mode = is_ai_campaign(campaign)
        if ai_mode:
            path = select_content_path(campaign=campaign, lead=lead, phone=phone)
            if path.path == "ineligible":
                # open_window_only closed window → skip (do not silent-template)
                if path.reason_code == "skipped_closed_window":
                    db.campaign_recipients.update_one(
                        {"_id": recipient["_id"]},
                        {
                            "$set": {
                                "status": "skipped",
                                "error_code": "skipped_closed_window",
                                "error_message": path.safe_message,
                                "updated_at": _utcnow(),
                            }
                        },
                    )
                    _refresh_campaign_counters(db, user_id, campaign_id)
                    _maybe_complete_campaign(db, user_id, campaign_id)
                    return
                raise RuntimeError(path.safe_message)
            # Approval gate
            review_mode = campaign.get("review_mode") or "sample_review"
            if review_mode != "no_manual_review" and not recipient.get("ai_approved"):
                # sample_review: campaign-level approved_at allows send of generated content
                if review_mode == "sample_review" and campaign.get("approved_at"):
                    pass
                elif review_mode == "full_review":
                    raise RuntimeError("Recipient AI content not approved")
            # If free-form/KB was generated but window closed now → only template-fallback when scope allows
            prior_freeform = recipient.get("content_source") in ("ai_freeform", "knowledge_base")
            if path.path != "ai_freeform" and prior_freeform:
                scope = resolve_delivery_scope(campaign)
                action = campaign.get("on_window_closed_before_send") or (
                    "skip" if scope == "open_window_only" else "use_static_template"
                )
                if scope == "open_window_only" or action == "skip":
                    db.campaign_recipients.update_one(
                        {"_id": recipient["_id"]},
                        {
                            "$set": {
                                "status": "skipped",
                                "error_code": "skipped_closed_window",
                                "error_message": "Window closed before send — open_window_only",
                                "updated_at": _utcnow(),
                            }
                        },
                    )
                    _refresh_campaign_counters(db, user_id, campaign_id)
                    _maybe_complete_campaign(db, user_id, campaign_id)
                    return
                if action == "require_manual_review":
                    db.campaign_recipients.update_one(
                        {"_id": recipient["_id"]},
                        {
                            "$set": {
                                "status": "skipped",
                                "error_message": "needs_manual_review",
                                "ai_generation_status": "needs_review",
                                "updated_at": _utcnow(),
                            }
                        },
                    )
                    _refresh_campaign_counters(db, user_id, campaign_id)
                    return
                # use_static_template / personalised vars path (all_eligible_recipients)
                tid, sid = resolve_fallback_template(campaign)
                if not tid and not sid:
                    db.campaign_recipients.update_one(
                        {"_id": recipient["_id"]},
                        {
                            "$set": {
                                "status": "skipped",
                                "error_code": "skipped_closed_window",
                                "error_message": "Window closed before send — no approved fallback template",
                                "updated_at": _utcnow(),
                            }
                        },
                    )
                    _refresh_campaign_counters(db, user_id, campaign_id)
                    _maybe_complete_campaign(db, user_id, campaign_id)
                    return
                template_id = tid or template_id
                content_sid = sid or content_sid
                has_template = bool(content_sid or template_id)

        elig = get_whatsapp_send_eligibility(
            lead=lead or {"phone": phone},
            phone=phone,
            purpose="campaign",
            has_template=has_template,
        )
        if not elig.allowed:
            try:
                from app.observability.metrics import inc_policy_blocked
                inc_policy_blocked(elig.reason_code)
            except Exception:
                pass
            raise RuntimeError(elig.safe_message)

        if not acquire_send_permit(user_id=user_id, priority="bulk"):
            db.campaign_recipients.update_one(
                {"_id": recipient["_id"]},
                {"$set": {"status": "queued", "updated_at": _utcnow()}, "$inc": {"attempt_count": -1}},
            )
            _queue().enqueue_in(
                timedelta(seconds=3),
                send_campaign_recipient,
                user_id,
                campaign_id,
                recipient_id,
            )
            return

        try:
            body = (campaign.get("message") or "").strip() or None
            content_variables = campaign.get("content_variables")
            if ai_mode:
                src = recipient.get("content_source")
                # Recompute desired source from live path
                live_path = select_content_path(campaign=campaign, lead=lead, phone=phone).path
                if live_path == "ai_freeform" and recipient.get("generated_message"):
                    body = (recipient.get("generated_message") or "").strip()
                    content_sid = None
                    template_id = None
                    content_variables = None
                elif (
                    recipient.get("content_source") == "knowledge_base"
                    and recipient.get("generated_message")
                    and live_path == "ai_freeform"
                ):
                    body = (recipient.get("generated_message") or "").strip()
                    content_sid = None
                    template_id = None
                    content_variables = None
                elif live_path in ("ai_template_variables", "template"):
                    body = None
                    tid, sid = resolve_fallback_template(campaign)
                    template_id = tid or template_id
                    content_sid = sid or content_sid or recipient.get("fallback_template_content_sid")
                    from app.services.ai_campaign import prepare_template_variables_for_send

                    content_variables = prepare_template_variables_for_send(
                        db,
                        user_id=user_id,
                        campaign=campaign,
                        lead=lead,
                        template_id=template_id,
                        generated=recipient.get("generated_template_variables"),
                    )
                    # content_variables may be None when the approved template has no placeholders —
                    # that is valid and must not be treated as a send failure.
                else:
                    raise RuntimeError("No sendable AI content path")
                _ = src

            if body in ("[media]",):
                body = None
            media_url = campaign.get("media_url") if not content_sid else None

            if content_sid or template_id:
                if template_id and ObjectId.is_valid(str(template_id)):
                    tmpl = db.templates.find_one({"_id": ObjectId(str(template_id)), "user_id": user_id})
                    if not tmpl or tmpl.get("status") != "approved" or not tmpl.get("content_sid"):
                        raise RuntimeError("Template is not approved")
                    content_sid = tmpl["content_sid"]
                if not content_sid:
                    raise RuntimeError("Missing content_sid")
                result = twilio_service.send_whatsapp(
                    phone,
                    content_sid=content_sid,
                    content_variables=content_variables,
                )
                display = body or f"[template:{content_sid}]"
            else:
                if not is_whatsapp_window_open(lead):
                    raise RuntimeError(WINDOW_CLOSED_ERROR)
                resolved_media = _resolve_media_url(media_url) if media_url else None
                if not body and not resolved_media:
                    raise RuntimeError("empty message")
                result = twilio_service.send_whatsapp(phone, body=body, media_url=resolved_media)
                display = body or "[media]"

            try:
                from app.observability.metrics import inc_campaign_send, inc_outbound
                inc_campaign_send()
                inc_outbound(ok=True)
            except Exception:
                pass

            msg_doc = {
                "user_id": user_id,
                "lead_id": recipient.get("lead_id"),
                "direction": "outbound",
                "message": display or "[campaign]",
                "status": "sent",
                "twilio_sid": result.get("sid"),
                "campaign_id": campaign_id,
                "campaign_recipient_id": str(recipient["_id"]),
                "message_purpose": "campaign",
                "consent_status_at_send": elig.consent_status,
                "policy_decision": "allowed",
                "policy_reason": elig.reason_code,
                "template_id": str(template_id) if template_id else None,
                "content_sid": content_sid,
                "created_at": _utcnow(),
            }
            if media_url:
                msg_doc["media_url"] = media_url
            ins = db.messages.insert_one(msg_doc)

            updated = db.campaign_recipients.find_one_and_update(
                {"_id": recipient["_id"], "status": "processing"},
                {
                    "$set": {
                        "status": "sent",
                        "message_id": str(ins.inserted_id),
                        "twilio_sid": result.get("sid"),
                        "message_purpose": "campaign",
                        "error_code": None,
                        "error_message": None,
                        "updated_at": _utcnow(),
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if updated:
                _publish(user_id, "campaign:recipient_updated", _serialize(updated))
            _refresh_campaign_counters(db, user_id, campaign_id)
            _maybe_complete_campaign(db, user_id, campaign_id)
        finally:
            release_send_permit()

    except Exception as exc:
        err = str(exc)[:500]
        category = classify_send_error(exc)
        attempts = int(recipient.get("attempt_count") or 1)
        max_r = max(wa_max_retries(), int(getattr(settings, "CAMPAIGN_MAX_RETRIES", 3)))
        campaign = db.campaigns.find_one({"_id": ObjectId(campaign_id)})
        try:
            from app.observability.metrics import inc_outbound, inc_provider_failure
            inc_outbound(ok=False)
            inc_provider_failure(category)
        except Exception:
            pass
        if campaign and campaign.get("status") == "cancelled":
            db.campaign_recipients.update_one(
                {"_id": recipient["_id"]},
                {"$set": {"status": "cancelled", "error_message": err, "updated_at": _utcnow()}},
            )
        elif is_retryable_category(category) and attempts <= max_r and campaign and campaign.get("status") == "running":
            delay = compute_retry_delay_seconds(attempts)
            next_at = _utcnow() + timedelta(seconds=delay)
            updated = db.campaign_recipients.find_one_and_update(
                {"_id": recipient["_id"]},
                {
                    "$set": {
                        "status": "retrying",
                        "error_message": err,
                        "error_code": category,
                        "next_retry_at": next_at,
                        "updated_at": _utcnow(),
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            try:
                from app.observability.metrics import inc_retry_scheduled
                inc_retry_scheduled()
            except Exception:
                pass
            if updated:
                _publish(user_id, "campaign:recipient_updated", _serialize(updated))
                _queue().enqueue_in(
                    timedelta(seconds=delay),
                    send_campaign_recipient,
                    user_id,
                    campaign_id,
                    str(recipient["_id"]),
                )
        else:
            err_l = err.lower()
            error_code = category
            if (
                "skipped_closed_window" in err_l
                or category == "window_closed"
                or WINDOW_CLOSED_ERROR.lower() in err_l
            ):
                final_status = "skipped"
                # Prefer stable reason code for open-window-only campaigns
                if "skipped_closed_window" in err_l or (
                    campaign
                    and (campaign.get("delivery_scope") or "") == "open_window_only"
                    and category == "window_closed"
                ):
                    error_code = "skipped_closed_window"
            elif category in ("consent_blocked", "consent_required") or "blacklist" in err_l or "opt" in err_l:
                final_status = "skipped"
            else:
                final_status = "failed"
            try:
                from app.observability.metrics import inc_retry_exhausted
                if attempts > max_r:
                    inc_retry_exhausted()
            except Exception:
                pass
            updated = db.campaign_recipients.find_one_and_update(
                {"_id": recipient["_id"]},
                {
                    "$set": {
                        "status": final_status,
                        "error_message": (
                            "Campaign limited to open WhatsApp windows — recipient skipped"
                            if error_code == "skipped_closed_window"
                            else err
                        ),
                        "error_code": error_code,
                        "updated_at": _utcnow(),
                    }
                },
                return_document=ReturnDocument.AFTER,
            )
            if updated:
                _publish(user_id, "campaign:recipient_updated", _serialize(updated))
        _refresh_campaign_counters(db, user_id, campaign_id)
        _maybe_complete_campaign(db, user_id, campaign_id)


def finalize_campaign_if_idle(user_id: str, campaign_id: str) -> None:
    _maybe_complete_campaign(_db(), user_id, campaign_id)


def process_due_scheduled_campaigns() -> int:
    """Find scheduled campaigns due now and start them. Returns count started."""
    db = _db()
    now = _utcnow()
    due = list(
        db.campaigns.find(
            {"status": "scheduled", "scheduled_at": {"$lte": now}}
        ).limit(50)
    )
    started = 0
    q = _queue()
    from app.services.ai_campaign import is_ai_campaign

    for c in due:
        res = db.campaigns.update_one(
            {"_id": c["_id"], "status": "scheduled"},
            {"$set": {"status": "queued", "updated_at": now}},
        )
        if res.modified_count:
            if is_ai_campaign(c):
                q.enqueue(start_ai_campaign_job, str(c["user_id"]), str(c["_id"]))
            else:
                q.enqueue(start_campaign_job, str(c["user_id"]), str(c["_id"]))
            started += 1
    return started


def start_ai_campaign_job(user_id: str, campaign_id: str) -> None:
    """Generate AI content in batches, then hand off to existing send batcher."""
    db = _db()
    campaign = db.campaigns.find_one({"_id": ObjectId(campaign_id), "user_id": user_id})
    if not campaign:
        return
    if campaign.get("status") not in ("queued", "running", "draft", "scheduled"):
        if campaign.get("status") != "running":
            return
    now = _utcnow()
    db.campaigns.update_one(
        {"_id": ObjectId(campaign_id)},
        {
            "$set": {
                "status": "running",
                "ai_generation_status": "generating",
                "ai_generation_started_at": campaign.get("ai_generation_started_at") or now,
                "started_at": campaign.get("started_at") or now,
                "updated_at": now,
                "last_error": None,
            }
        },
    )
    db.campaign_recipients.update_many(
        {"campaign_id": campaign_id, "status": "pending"},
        {"$set": {"status": "queued", "updated_at": now}},
    )
    _publish(user_id, "campaign:started", _serialize(db.campaigns.find_one({"_id": ObjectId(campaign_id)})))
    process_ai_generation_batch(user_id, campaign_id)


def process_ai_generation_batch(user_id: str, campaign_id: str) -> None:
    from app.services.activity import record_activity_sync
    from app.services.ai_campaign import (
        apply_generation_to_recipient,
        generate_campaign_content,
        recipient_ai_idempotency_key,
        select_content_path,
    )
    from app.services.idempotency import claim_idempotency
    from app.services.notifications import create_notification_sync

    db = _db()
    campaign = db.campaigns.find_one({"_id": ObjectId(campaign_id), "user_id": user_id})
    if not campaign:
        return
    if campaign.get("status") in ("paused", "cancelled") or campaign.get("status") in TERMINAL_STATUSES:
        return
    if campaign.get("status") != "running":
        return

    batch_size = max(1, min(25, int(getattr(settings, "CAMPAIGN_BATCH_SIZE", 25))))
    cur = (
        db.campaign_recipients.find(
            {
                "campaign_id": campaign_id,
                "user_id": user_id,
                "status": {"$in": ["queued", "pending"]},
                "$or": [
                    {"ai_generation_status": {"$in": ["pending", "failed", None]}},
                    {"ai_generation_status": {"$exists": False}},
                    {
                        "ai_generation_status": "ready",
                        "generated_message": None,
                        "generated_template_variables": None,
                        "content_source": {"$ne": "template"},
                    },
                ],
            }
        )
        .sort("created_at", 1)
        .limit(batch_size)
    )
    recipients = list(cur)
    if not recipients:
        # Generation done — start sending
        db.campaigns.update_one(
            {"_id": ObjectId(campaign_id)},
            {
                "$set": {
                    "ai_generation_status": "ready",
                    "ai_generation_completed_at": _utcnow(),
                    "updated_at": _utcnow(),
                }
            },
        )
        try:
            create_notification_sync(
                db,
                user_id=user_id,
                type="campaign_ai_generation_complete",
                title="Campaign AI generation complete",
                message="Generated content is ready; sending will continue",
                resource_type="campaign",
                resource_id=campaign_id,
                dedupe_key=f"camp_ai_gen_done:{campaign_id}",
            )
        except Exception:
            pass
        record_activity_sync(
            db,
            tenant_id=user_id,
            event_type="campaign.ai_generation_completed",
            summary="AI campaign generation completed",
            resource_type="campaign",
            resource_id=campaign_id,
        )
        process_campaign_batch(user_id, campaign_id)
        return

    snap = campaign.get("agent_snapshot")
    for r in recipients:
        if campaign.get("status") == "paused":
            return
        camp_fresh = db.campaigns.find_one({"_id": ObjectId(campaign_id)})
        if not camp_fresh or camp_fresh.get("status") != "running":
            return
        rid = str(r["_id"])
        version = max(1, int(r.get("generation_version") or 0) + 1)
        idem = recipient_ai_idempotency_key(campaign_id, rid, version)
        if not claim_idempotency(user_id, idem):
            continue
        lead = None
        if r.get("lead_id") and ObjectId.is_valid(str(r["lead_id"])):
            lead = db.leads.find_one({"_id": ObjectId(str(r["lead_id"])), "user_id": user_id})
        if not lead:
            lead = db.leads.find_one({"user_id": user_id, "phone": r.get("phone")})
        path = select_content_path(campaign=campaign, lead=lead, phone=r.get("phone"))
        if path.path == "ineligible":
            # Intentional policy skips (closed window, opt-out) are not generation failures
            intentional = path.reason_code in (
                "skipped_closed_window",
                "closed_window_missing_template",
                "consent_blocked",
                "consent_required",
                "window_closed",
            )
            # Normalize: open_window_only must never surface closed_window_missing_template
            reason = path.reason_code
            if (
                (campaign.get("delivery_scope") or "") == "open_window_only"
                and reason in ("closed_window_missing_template", "window_closed")
            ):
                reason = "skipped_closed_window"
            db.campaign_recipients.update_one(
                {"_id": r["_id"]},
                {
                    "$set": {
                        "status": "skipped",
                        "error_code": reason,
                        "error_message": path.safe_message if intentional else reason,
                        "ai_generation_status": "skipped" if intentional else "failed",
                        "ai_generation_error_category": reason,
                        "updated_at": _utcnow(),
                    }
                },
            )
            continue
        if path.path == "template":
            tid_sid = campaign.get("fallback_template_content_sid") or campaign.get("content_sid")
            db.campaign_recipients.update_one(
                {"_id": r["_id"]},
                {
                    "$set": {
                        "content_source": "template",
                        "ai_generation_status": "ready",
                        "ai_approved": True if (campaign.get("review_mode") == "no_manual_review" or campaign.get("approved_at")) else False,
                        "generated_template_variables": campaign.get("content_variables") or {},
                        "fallback_template_content_sid": tid_sid,
                        "ai_idempotency_key": idem,
                        "generation_version": version,
                        "updated_at": _utcnow(),
                    }
                },
            )
            continue

        gen = generate_campaign_content(
            db,
            user_id=user_id,
            campaign=campaign,
            recipient=r,
            lead=lead,
            agent_snapshot=snap,
            preview=False,
        )
        fields = apply_generation_to_recipient(recipient=r, gen=gen, version_inc=True)
        fields["ai_idempotency_key"] = idem
        fields["is_preview_only"] = False
        if gen.ok and (
            campaign.get("review_mode") == "no_manual_review" or campaign.get("approved_at")
        ):
            fields["ai_approved"] = True
            fields["ai_approved_at"] = _utcnow()
            fields["ai_generation_status"] = "ready"
        if not gen.ok:
            # Policy skips from content-path must not count as AI generation failures
            if gen.error_category in (
                "skipped_closed_window",
                "closed_window_missing_template",
                "consent_blocked",
                "consent_required",
                "window_closed",
            ):
                reason = gen.error_category
                if (
                    (campaign.get("delivery_scope") or "") == "open_window_only"
                    and reason in ("closed_window_missing_template", "window_closed")
                ):
                    reason = "skipped_closed_window"
                fields.update(
                    {
                        "status": "skipped",
                        "error_code": reason,
                        "error_message": (
                            "Campaign limited to open WhatsApp windows — recipient skipped"
                            if reason == "skipped_closed_window"
                            else (gen.error_category or reason)
                        ),
                        "ai_generation_status": "skipped",
                        "ai_generation_error_category": reason,
                        "ai_approved": False,
                        "content_source": None,
                        "generated_message": None,
                    }
                )
                db.campaign_recipients.update_one({"_id": r["_id"]}, {"$set": fields})
                continue
            action = campaign.get("on_ai_failure") or "use_static_template"
            if gen.error_category in ("moderation", "content_blocked", "quality_rejected", "price_above_ceiling"):
                action = campaign.get("on_moderation_block") or "require_manual_review"
            if gen.error_category == "quota_exceeded":
                action = campaign.get("on_quota_exceeded") or "require_manual_review"
            if action == "use_static_template" and (
                campaign.get("fallback_template_content_sid") or campaign.get("content_sid")
            ):
                fields.update(
                    {
                        "content_source": "template",
                        "ai_generation_status": "ready",
                        "generated_message": None,
                        "generated_template_variables": campaign.get("content_variables") or {},
                        "ai_approved": bool(
                            campaign.get("review_mode") == "no_manual_review" or campaign.get("approved_at")
                        ),
                    }
                )
            elif action == "skip":
                fields.update(
                    {
                        "status": "skipped",
                        "error_code": gen.error_category or "ai_failed",
                        "error_message": gen.error_category or "ai_failed",
                    }
                )
            else:
                fields["ai_generation_status"] = "needs_review"
        db.campaign_recipients.update_one({"_id": r["_id"]}, {"$set": fields})
        # accumulate token counters
        db.campaigns.update_one(
            {"_id": ObjectId(campaign_id)},
            {
                "$inc": {
                    "ai_input_tokens_total": int(gen.input_tokens or 0),
                    "ai_output_tokens_total": int(gen.output_tokens or 0),
                    "ai_estimated_cost_total": float(gen.estimated_cost or 0),
                }
            },
        )

    _refresh_campaign_counters(db, user_id, campaign_id)
    # Chain next generation batch
    _queue().enqueue_in(timedelta(seconds=1), process_ai_generation_batch, user_id, campaign_id)
