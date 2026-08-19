"""Sync RQ tasks. These run in the worker process and use sync Mongo + Redis."""
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from bson import ObjectId
from pymongo import MongoClient, ReturnDocument
from redis import Redis

from app.config import settings
from app.services import twilio_service, openai_service
from app.services.whatsapp_outbound import send_whatsapp_text, send_whatsapp_template
from app.services.whatsapp_window import (
    WINDOW_CLOSED_ERROR,
    is_whatsapp_window_open,
)
from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility
from app.services.throughput import acquire_send_permit, release_send_permit
from app.services.idempotency import claim_idempotency, make_idempotency_key
from app.services.twilio_errors import classify_send_error, is_retryable_category
from app.services.retry_backoff import compute_retry_delay_seconds, max_retries
from app.services.phone_norm import normalize_e164

logger = logging.getLogger(__name__)

_mongo_client: MongoClient | None = None
_redis_client: Redis | None = None


def _db():
    global _mongo_client
    if _mongo_client is None:
        _mongo_client = MongoClient(settings.MONGO_URI)
    return _mongo_client[settings.MONGO_DB]


def _redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.REDIS_URL,
            health_check_interval=60,
            socket_keepalive=True,
            retry_on_timeout=True,
        )
    return _redis_client


def _publish(user_id: str, event: str, data: dict) -> None:
    payload = json.dumps({"event": event, "data": data, "user_id": user_id})
    _redis().publish("ws:events", payload)


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


def _norm_phone(phone: str | None) -> str | None:
    """Backward-compatible alias for canonical E.164 normalisation."""
    return normalize_e164(phone)


def _fail_message(
    db,
    message_id: ObjectId,
    user_id: str,
    error: str,
    status: str = "failed",
    *,
    reason_code: str | None = None,
) -> None:
    db.messages.update_one(
        {"_id": message_id},
        {
            "$set": {
                "status": status,
                "error": error[:500],
                "final_failure_reason": (reason_code or error)[:200],
                "policy_reason": reason_code,
            }
        },
    )
    msg = db.messages.find_one({"_id": message_id})
    if msg:
        _publish(user_id, "message:updated", _serialize(msg))


def _resolve_media_url(media_url: Optional[str]) -> Optional[str]:
    """Twilio needs an absolute public URL for outbound media."""
    if not media_url:
        return None
    url = media_url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "PUBLIC_BASE_URL is required to send media (Twilio must fetch the file)"
        )
    if not url.startswith("/"):
        url = "/" + url
    return f"{base}{url}"


def _schedule_message_retry(message_id: str, user_id: str, lead_id: str, attempt: int, **send_kwargs) -> bool:
    if attempt > max_retries():
        return False
    delay = compute_retry_delay_seconds(attempt)
    from app.workers.queue import get_queue
    from rq import Queue

    q: Queue = get_queue(settings.RQ_HIGH_QUEUE_NAME)
    q.enqueue_in(
        timedelta(seconds=delay),
        send_outbound_message,
        message_id,
        user_id,
        lead_id,
        send_kwargs.get("body"),
        media_url=send_kwargs.get("media_url"),
        content_sid=send_kwargs.get("content_sid"),
        content_variables=send_kwargs.get("content_variables"),
        _retry_attempt=attempt,
    )
    try:
        from app.observability.metrics import inc_retry_scheduled

        inc_retry_scheduled()
    except Exception:
        pass
    return True


def _resolve_ai_send_provider(
    db,
    user_id: str,
    lead_id: str,
    provider: str | None,
    trigger_message_id: str | None,
) -> tuple[str | None, str | None, dict | None]:
    """Resolve send provider from job args + the triggering inbound. Never uses env."""
    explicit = (provider or "").strip().lower() or None
    trigger_id = (trigger_message_id or "").strip() or None
    trigger_doc = None
    if trigger_id:
        if not ObjectId.is_valid(trigger_id):
            logger.warning(
                "ai_reply invalid trigger_message_id user_id=%s lead_id=%s",
                user_id,
                lead_id,
            )
            return None, trigger_id, None
        trigger_doc = db.messages.find_one(
            {
                "_id": ObjectId(trigger_id),
                "user_id": user_id,
                "lead_id": lead_id,
                "direction": "inbound",
            }
        )
        if not trigger_doc:
            logger.warning(
                "ai_reply trigger message not found user_id=%s lead_id=%s",
                user_id,
                lead_id,
            )
            return None, trigger_id, None
        stored = (trigger_doc.get("provider") or "").strip().lower() or None
        if explicit and stored and explicit != stored:
            logger.warning(
                "ai_reply provider conflict explicit=%s stored=%s user_id=%s lead_id=%s",
                explicit,
                stored,
                user_id,
                lead_id,
            )
            return None, trigger_id, trigger_doc
        resolved = explicit or stored
        if resolved not in ("twilio", "meta"):
            logger.warning("ai_reply unknown trigger provider=%s", resolved)
            return None, trigger_id, trigger_doc
        return resolved, trigger_id, trigger_doc
    if explicit in (None, "twilio"):
        return "twilio", None, None
    logger.warning(
        "ai_reply provider=%s without trigger_message_id — refusing send",
        explicit,
    )
    return None, None, None


def _classify_ai_outbound_error(exc: BaseException, *, provider: str) -> str:
    if provider == "meta":
        from app.services.meta_whatsapp_service import MetaWhatsAppError
        from app.services.whatsapp_outbound import UnknownWhatsAppProviderError

        if isinstance(exc, UnknownWhatsAppProviderError):
            return "configuration_error"
        if isinstance(exc, MetaWhatsAppError):
            code = exc.status_code
            if code == 429:
                return "provider_rate_limited"
            if code is not None and 400 <= int(code) < 500:
                return "non_retryable"
    return classify_send_error(exc)


def send_outbound_message(
    message_id: str,
    user_id: str,
    lead_id: str,
    body: Optional[str] = None,
    media_url: Optional[str] = None,
    content_sid: Optional[str] = None,
    content_variables: Optional[dict] = None,
    _retry_attempt: int = 0,
) -> None:
    db = _db()
    mid = ObjectId(message_id)
    msg = db.messages.find_one({"_id": mid, "user_id": user_id})
    if not msg:
        return
    if msg.get("status") in ("sent", "delivered", "read", "failed", "canceled", "cancelled"):
        return

    idem = make_idempotency_key("outbound", user_id, message_id)
    if _retry_attempt == 0 and not claim_idempotency(user_id, idem):
        # Another worker already claimed this send
        try:
            from app.observability.metrics import inc_duplicate_prevented

            inc_duplicate_prevented()
        except Exception:
            pass
        return

    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if not lead or not lead.get("phone"):
        _fail_message(db, mid, user_id, "missing phone", reason_code="invalid_recipient")
        return

    msg_provider = (msg.get("provider") or "").strip().lower() or "twilio"
    if msg_provider not in ("twilio", "meta"):
        _fail_message(
            db,
            mid,
            user_id,
            f"Unknown WhatsApp provider: {msg_provider}",
            reason_code="configuration_error",
        )
        return
    if msg_provider == "meta" and (
        media_url
        or msg.get("media_url")
    ):
        _fail_message(
            db,
            mid,
            user_id,
            "Meta media is not supported yet",
            reason_code="non_retryable",
        )
        return
    if msg_provider == "meta" and (content_sid or msg.get("content_sid")):
        _fail_message(
            db,
            mid,
            user_id,
            "Twilio Content templates cannot be sent on Meta WhatsApp conversations",
            reason_code="non_retryable",
        )
        return

    is_meta_template = msg_provider == "meta" and (
        (msg.get("message_type") or "") == "template" or bool(msg.get("meta_template_name"))
    )

    purpose = msg.get("message_purpose") or "conversational"
    elig = get_whatsapp_send_eligibility(
        lead=lead,
        purpose=purpose,
        has_template=bool(content_sid) or is_meta_template,
        has_media=bool(media_url),
        provider=msg_provider,
    )
    if not elig.allowed:
        try:
            from app.observability.metrics import inc_policy_blocked

            inc_policy_blocked(elig.reason_code)
        except Exception:
            pass
        _fail_message(db, mid, user_id, elig.safe_message, reason_code=elig.reason_code)
        return

    if not acquire_send_permit(user_id=user_id, priority="high"):
        # Defer without failing
        from app.workers.queue import get_queue

        get_queue(settings.RQ_HIGH_QUEUE_NAME).enqueue_in(
            timedelta(seconds=2),
            send_outbound_message,
            message_id,
            user_id,
            lead_id,
            body,
            media_url=media_url,
            content_sid=content_sid,
            content_variables=content_variables,
            _retry_attempt=_retry_attempt,
        )
        return

    try:
        resolved_media = None if content_sid else _resolve_media_url(media_url)
        send_body = body
        if not content_sid and not is_meta_template and not send_body and resolved_media:
            send_body = None
        if not content_sid and not is_meta_template and not send_body and not resolved_media:
            _fail_message(db, mid, user_id, "empty message")
            return

        db.messages.update_one({"_id": mid}, {"$set": {"status": "sending", "retry_count": _retry_attempt}})
        if msg_provider == "meta":
            user = db.users.find_one({"_id": ObjectId(user_id)})
            if is_meta_template:
                from app.services.meta_templates import MetaTemplateError, build_graph_components

                tid = msg.get("template_id")
                tmpl = None
                if tid and ObjectId.is_valid(str(tid)):
                    tmpl = db.templates.find_one(
                        {"_id": ObjectId(str(tid)), "user_id": user_id, "provider": "meta"}
                    )
                if not tmpl:
                    _fail_message(db, mid, user_id, "Meta template not found", reason_code="non_retryable")
                    return
                try:
                    components = build_graph_components(
                        template=tmpl,
                        content_variables=content_variables or msg.get("content_variables"),
                    )
                except MetaTemplateError as exc:
                    _fail_message(db, mid, user_id, str(exc), reason_code="non_retryable")
                    return
                result = send_whatsapp_template(
                    provider="meta",
                    to=lead["phone"],
                    name=(tmpl.get("meta_template_name") or msg.get("meta_template_name") or ""),
                    language_code=(tmpl.get("meta_language_code") or msg.get("meta_language_code") or ""),
                    components=components,
                    user=user,
                )
            else:
                result = send_whatsapp_text(
                    provider="meta",
                    to=lead["phone"],
                    text=send_body or "",
                    user=user,
                )
            db.messages.update_one(
                {"_id": mid},
                {
                    "$set": {
                        "status": "sent",
                        "provider": "meta",
                        "provider_message_id": result.get("provider_message_id"),
                        "provider_status": result.get("status"),
                        "consent_status_at_send": elig.consent_status,
                        "window_open_at_send": elig.window_status == "open",
                        "policy_decision": "allowed",
                    }
                },
            )
        else:
            result = twilio_service.send_whatsapp(
                lead["phone"],
                body=send_body if not content_sid else None,
                media_url=resolved_media,
                content_sid=content_sid,
                content_variables=content_variables,
            )
            provider_status = (result.get("status") or "").strip().lower()
            app_status = "sent" if provider_status in ("", "queued", "accepted") else provider_status
            sid = result.get("sid")
            db.messages.update_one(
                {"_id": mid},
                {
                    "$set": {
                        "status": app_status,
                        "provider": "twilio",
                        "provider_status": result.get("status"),
                        "provider_message_id": sid,
                        "twilio_sid": sid,
                        "consent_status_at_send": elig.consent_status,
                        "window_open_at_send": elig.window_status == "open",
                        "policy_decision": "allowed",
                        "sender_number": (settings.TWILIO_WHATSAPP_FROM or "")[:40] or None,
                    }
                },
            )
        updated = db.messages.find_one({"_id": mid})
        if updated:
            _publish(user_id, "message:updated", _serialize(updated))
        try:
            from app.observability.metrics import inc_outbound

            inc_outbound(ok=True)
        except Exception:
            pass
    except Exception as exc:
        category = _classify_ai_outbound_error(exc, provider=msg_provider)
        try:
            from app.observability.metrics import inc_outbound, inc_provider_failure

            inc_outbound(ok=False)
            inc_provider_failure(category)
        except Exception:
            pass
        if is_retryable_category(category) and _retry_attempt < max_retries():
            db.messages.update_one(
                {"_id": mid},
                {
                    "$set": {
                        "status": "queued",
                        "error": str(exc)[:500],
                        "retry_count": _retry_attempt + 1,
                    }
                },
            )
            if _schedule_message_retry(
                message_id,
                user_id,
                lead_id,
                _retry_attempt + 1,
                body=body,
                media_url=media_url,
                content_sid=content_sid,
                content_variables=content_variables,
            ):
                return
            try:
                from app.observability.metrics import inc_retry_exhausted

                inc_retry_exhausted()
            except Exception:
                pass
        _fail_message(db, mid, user_id, str(exc), reason_code=category)
    finally:
        release_send_permit()


def send_welcome_and_terms(user_id: str, lead_id: str) -> None:
    """B11: send welcome + T&C to a brand-new lead via the worker (not the webhook request).

    Idempotent per (user, lead) — safe to enqueue multiple times (e.g. RQ retries).
    """
    db = _db()
    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if not lead or not lead.get("phone"):
        return

    if lead.get("welcome_sent_at") or lead.get("welcome_terms_status") == "sent":
        return

    idem = make_idempotency_key("welcome", user_id, lead_id)
    if not claim_idempotency(user_id, idem):
        try:
            from app.observability.metrics import inc_duplicate_prevented

            inc_duplicate_prevented()
        except Exception:
            pass
        return

    db.leads.update_one(
        {"_id": ObjectId(lead_id)},
        {"$set": {"welcome_terms_status": "sending"}},
    )

    user = db.users.find_one({"_id": ObjectId(user_id)})
    agent = db.agents.find_one({"user_id": user_id, "status": "active"}, sort=[("updated_at", -1)])
    welcome = (agent or {}).get("welcome_message") or ""
    terms = (agent or {}).get("terms_text") or ""

    if not welcome and not terms:
        db.leads.update_one(
            {"_id": ObjectId(lead_id)},
            {"$set": {"welcome_terms_status": "skipped_no_content"}},
        )
        return

    # Re-check eligibility at send time — lead state may have changed between
    # webhook enqueue and worker pickup (opt-out, blacklist, etc.).
    elig = get_whatsapp_send_eligibility(
        lead=lead, purpose="transactional", has_template=False
    )
    if not elig.allowed:
        db.leads.update_one(
            {"_id": ObjectId(lead_id)},
            {"$set": {"welcome_terms_status": "blocked", "welcome_error": elig.safe_message}},
        )
        try:
            from app.observability.metrics import inc_policy_blocked

            inc_policy_blocked(elig.reason_code)
        except Exception:
            pass
        return

    sent_any = False
    last_error: Optional[str] = None
    for text, purpose in ((welcome, "transactional"), (terms, "transactional")):
        if not text:
            continue
        try:
            result = twilio_service.send_whatsapp(lead["phone"], body=text)
            msg_doc = {
                "user_id": user_id,
                "lead_id": lead_id,
                "direction": "outbound",
                "message": text,
                "status": result.get("status") or "sent",
                "twilio_sid": result.get("sid"),
                "message_purpose": purpose,
                "consent_status_at_send": elig.consent_status,
                "created_at": datetime.now(timezone.utc),
            }
            res = db.messages.insert_one(msg_doc)
            msg_doc["_id"] = res.inserted_id
            _publish(user_id, "message:new", _serialize(msg_doc))
            sent_any = True
            try:
                from app.observability.metrics import inc_outbound

                inc_outbound(ok=True)
            except Exception:
                pass
        except Exception as exc:
            last_error = str(exc)[:300]
            try:
                from app.observability.metrics import inc_outbound

                inc_outbound(ok=False)
            except Exception:
                pass
            logger.exception(
                "send_welcome_and_terms failed user_id=%s lead_id=%s", user_id, lead_id
            )

    update: dict = {}
    if sent_any:
        update["welcome_sent_at"] = datetime.now(timezone.utc)
        update["welcome_terms_status"] = "sent" if not last_error else "partial"
    else:
        update["welcome_terms_status"] = "failed"
    if last_error:
        update["welcome_error"] = last_error
    db.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": update})

    if last_error:
        try:
            from app.services.activity import record_activity_sync

            record_activity_sync(
                db,
                tenant_id=user_id,
                event_type="welcome.send_failed" if not sent_any else "welcome.send_partial",
                summary="Welcome/T&C send failed" if not sent_any else "Welcome/T&C partially sent",
                resource_type="lead",
                resource_id=lead_id,
                metadata={"error": last_error},
            )
        except Exception:
            logger.exception("record_activity_sync failed for welcome send")


def generate_and_send_ai_reply(
    user_id: str,
    lead_id: str,
    provider: str | None = None,
    trigger_message_id: str | None = None,
) -> None:
    db = _db()
    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if not lead:
        return
    from app.services.ai_config import resolve_ai_settings
    from app.services.ai_moderation import moderate_inbound, moderate_outbound
    from app.services.ai_quota import check_quota
    from app.services.ai_summary import get_summary, maybe_enqueue_summary
    from app.services.ai_context import load_conversation_context
    from app.services.notifications import create_notification_sync

    send_provider, trigger_id, trigger_doc = _resolve_ai_send_provider(
        db, user_id, lead_id, provider, trigger_message_id
    )
    if not send_provider:
        return

    user = db.users.find_one({"_id": ObjectId(user_id)})
    ai = resolve_ai_settings(user)
    if not ai.get("enabled"):
        return

    elig = get_whatsapp_send_eligibility(
        lead=lead, purpose="support", has_template=False, provider=send_provider
    )
    if not elig.allowed:
        logger.info(
            "ai_reply blocked by eligibility user_id=%s lead_id=%s provider=%s reason=%s",
            user_id,
            lead_id,
            send_provider,
            elig.reason_code,
        )
        return
    if lead.get("ai_paused") or lead.get("takeover_by"):
        return

    ok_q, q_reason = check_quota(user_id)
    if not ok_q:
        db.leads.update_one(
            {"_id": ObjectId(lead_id)},
            {"$set": {"needs_human": True, "last_ai_error_category": q_reason or "quota_exceeded"}},
        )
        create_notification_sync(
            db,
            user_id=user_id,
            type="system",
            title="AI quota exceeded",
            message="AI replies paused until quota resets. Human messaging remains available.",
            resource_type="ai_quota",
            resource_id=user_id,
            dedupe_key=f"ai_quota_block:{user_id}:{datetime.now(timezone.utc).strftime('%Y%m%d%H')}",
        )
        return

    if trigger_id:
        idem = make_idempotency_key("ai", user_id, lead_id, trigger_id)
    else:
        idem = make_idempotency_key("ai", user_id, lead_id, str(lead.get("last_inbound_at") or ""))
    if not claim_idempotency(user_id, idem):
        try:
            from app.observability.metrics import inc_duplicate_prevented

            inc_duplicate_prevented()
        except Exception:
            pass
        return

    last_in = trigger_doc
    if last_in is None:
        last_in = db.messages.find_one(
            {"user_id": user_id, "lead_id": lead_id, "direction": "inbound"},
            sort=[("created_at", -1)],
        )
    inbound_text = (last_in or {}).get("message") or ""
    if ai.get("moderation_enabled"):
        mod_in = moderate_inbound(inbound_text)
        if mod_in.categories:
            db.ai_events.insert_one(
                {
                    "tenant_id": user_id,
                    "event_type": "moderation_inbound",
                    "categories": mod_in.categories[:5],
                    "conversation_id": lead_id,
                    "created_at": datetime.now(timezone.utc),
                }
            )
        if mod_in.escalate or not mod_in.allowed:
            db.leads.update_one(
                {"_id": ObjectId(lead_id)},
                {"$set": {"needs_human": True, "updated_at": datetime.now(timezone.utc)}},
            )
            create_notification_sync(
                db,
                user_id=user_id,
                type="needs_human",
                title="Moderation escalation",
                message="A conversation needs human review after moderation.",
                resource_type="lead",
                resource_id=lead_id,
                dedupe_key=f"mod_esc:{lead_id}:{mod_in.reason or 'x'}",
            )
            if not mod_in.allowed:
                return

    try:
        from app.observability.metrics import inc_ai

        inc_ai(ok=True)
    except Exception:
        pass

    company = (user or {}).get("company_name")
    agent = db.agents.find_one({"user_id": user_id, "status": "active"}, sort=[("updated_at", -1)])
    agent_id = str(agent["_id"]) if agent and agent.get("_id") else None
    agent_name = ((agent or {}).get("name") or "").strip() or "AI Agent"
    summary_doc = get_summary(db, tenant_id=user_id, lead_id=lead_id)
    summary_text = (summary_doc or {}).get("summary")
    ctx = load_conversation_context(
        db, tenant_id=user_id, lead_id=lead_id, summary=summary_text
    )
    history = [
        {"role": m["role"], "content": m["content"], "direction": "inbound" if m["role"] == "user" else "outbound", "message": m["content"]}
        for m in ctx["messages"]
    ]

    fallback_sent_key = f"ai:fallback_sent:{user_id}:{lead_id}"
    try:
        reply = openai_service.generate_reply(
            agent,
            history,
            company,
            tenant_id=user_id,
            lead=lead,
            ai_settings=ai,
            summary=summary_text if ctx.get("summary_used") else summary_text,
            user=user,
        )
    except Exception as exc:
        logger.exception(
            "AI generate_reply failed user_id=%s lead_id=%s err=%s",
            user_id,
            lead_id,
            str(exc)[:200],
        )
        lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
        if lead and (lead.get("ai_paused") or lead.get("takeover_by")):
            return
        try:
            from app.observability.metrics import inc_ai

            inc_ai(ok=False)
        except Exception:
            pass
        cat = str(exc)[:80]
        db.leads.update_one(
            {"_id": ObjectId(lead_id)},
            {"$set": {"last_ai_error_category": cat, "updated_at": datetime.now(timezone.utc)}},
        )
        if settings.AI_FAILURE_MARK_NEEDS_HUMAN or cat in ("empty_response", "truncated_response"):
            db.leads.update_one({"_id": ObjectId(lead_id)}, {"$set": {"needs_human": True}})
        if settings.AI_FAILURE_FALLBACK_ENABLED:
            from redis import Redis

            r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
            if r.set(fallback_sent_key, "1", nx=True, ex=86400):
                reply = (settings.AI_FAILURE_FALLBACK_TEXT or "").strip()
            else:
                return
        else:
            return

    if not reply:
        logger.warning("AI reply empty user_id=%s lead_id=%s", user_id, lead_id)
        db.leads.update_one(
            {"_id": ObjectId(lead_id)},
            {
                "$set": {
                    "needs_human": True,
                    "last_ai_error_category": "empty_response",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        from redis import Redis

        r = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        if r.set(fallback_sent_key, "1", nx=True, ex=86400):
            reply = (settings.AI_FAILURE_FALLBACK_TEXT or "").strip()
            if not reply:
                return
        else:
            return

    if ai.get("moderation_enabled"):
        mod_out = moderate_outbound(reply, disallowed_topics=ai.get("ai_disallowed_topics") or "")
        if not mod_out.allowed:
            db.ai_events.insert_one(
                {
                    "tenant_id": user_id,
                    "event_type": "moderation_block",
                    "categories": mod_out.categories[:5],
                    "conversation_id": lead_id,
                    "created_at": datetime.now(timezone.utc),
                }
            )
            db.leads.update_one(
                {"_id": ObjectId(lead_id)},
                {"$set": {"needs_human": True, "last_ai_error_category": "content_blocked"}},
            )
            return

    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if not lead:
        return
    elig = get_whatsapp_send_eligibility(
        lead=lead, purpose="support", has_template=False, provider=send_provider
    )
    if (
        not elig.allowed
        or lead.get("ai_paused")
        or lead.get("takeover_by")
    ):
        return

    msg_doc = {
        "user_id": user_id,
        "lead_id": lead_id,
        "direction": "outbound",
        "message": reply,
        "status": "queued",
        "provider": send_provider,
        "provider_message_id": None,
        "twilio_sid": None,
        "error": None,
        "message_purpose": "support",
        "sender_type": "ai",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "consent_status_at_send": elig.consent_status,
        "trigger_message_id": trigger_id,
        "created_at": datetime.now(timezone.utc),
    }
    res = db.messages.insert_one(msg_doc)
    msg_doc["_id"] = res.inserted_id
    _publish(user_id, "message:new", _serialize(msg_doc))

    lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
    if not lead or lead.get("ai_paused") or lead.get("takeover_by"):
        _fail_message(
            db,
            res.inserted_id,
            user_id,
            "AI reply canceled — human takeover / AI paused",
            status="canceled",
        )
        return

    elig = get_whatsapp_send_eligibility(
        lead=lead, purpose="support", has_template=False, provider=send_provider
    )
    if not elig.allowed:
        _fail_message(db, res.inserted_id, user_id, elig.safe_message, status="canceled", reason_code=elig.reason_code)
        return

    msg_count = db.messages.count_documents({"user_id": user_id, "lead_id": lead_id})
    maybe_enqueue_summary(user_id, lead_id, msg_count)
    try:
        from app.workers.queue import enqueue
        from app.workers import ai_tasks

        if ai.get("extraction_enabled") and msg_count >= 4 and msg_count % 4 == 0:
            enqueue(ai_tasks.extract_lead_suggestions, user_id, lead_id, queue="bulk")
    except Exception:
        pass

    if not acquire_send_permit(user_id=user_id, priority="default"):
        from app.workers.queue import enqueue

        enqueue(
            send_outbound_message,
            str(res.inserted_id),
            user_id,
            lead_id,
            reply,
            queue="default",
        )
        return

    try:
        result = send_whatsapp_text(
            provider=send_provider,
            to=lead["phone"],
            text=reply,
            user=user,
        )
        set_fields: dict = {
            "status": "sent" if send_provider == "meta" else (result.get("status") or "sent"),
            "provider": send_provider,
            "provider_message_id": result.get("provider_message_id"),
            "policy_decision": "allowed",
        }
        if send_provider == "twilio":
            raw_status = (result.get("status") or "sent")
            set_fields["status"] = raw_status
            set_fields["twilio_sid"] = result.get("provider_message_id")
        db.messages.update_one(
            {"_id": res.inserted_id},
            {"$set": set_fields},
        )
        db.leads.update_one(
            {"_id": ObjectId(lead_id)},
            {
                "$set": {
                    "needs_human": False,
                    "last_ai_error_category": None,
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        msg = db.messages.find_one({"_id": res.inserted_id})
        if msg:
            _publish(user_id, "message:updated", _serialize(msg))
        lead_fresh = db.leads.find_one({"_id": ObjectId(lead_id)})
        if lead_fresh:
            _publish(user_id, "lead:updated", _serialize(lead_fresh))
        try:
            from app.observability.metrics import inc_outbound

            inc_outbound(ok=True)
        except Exception:
            pass
    except Exception as exc:
        cat = _classify_ai_outbound_error(exc, provider=send_provider)
        logger.warning(
            "AI outbound send failed user_id=%s lead_id=%s provider=%s cat=%s err=%s",
            user_id,
            lead_id,
            send_provider,
            cat,
            str(exc)[:200],
        )
        if is_retryable_category(cat):
            from app.workers.queue import enqueue

            db.messages.update_one(
                {"_id": res.inserted_id},
                {
                    "$set": {
                        "status": "queued",
                        "error": None,
                        "last_send_error": str(exc)[:300],
                        "policy_reason": cat,
                        "provider": send_provider,
                    }
                },
            )
            enqueue(
                send_outbound_message,
                str(res.inserted_id),
                user_id,
                lead_id,
                reply,
                queue="high",
            )
            msg = db.messages.find_one({"_id": res.inserted_id})
            if msg:
                _publish(user_id, "message:updated", _serialize(msg))
        else:
            _fail_message(db, res.inserted_id, user_id, str(exc), reason_code=cat)
    finally:
        release_send_permit()


# Blast recipient lifecycle: pending -> processing -> sent | retrying -> failed | cancelled
BLAST_OPEN_STATUSES = frozenset({"pending", "processing", "retrying"})
BLAST_TERMINAL_STATUSES = frozenset({"completed", "partially_completed", "failed", "cancelled"})


def _cancel_open_blast_recipients(db, blast_id: str) -> None:
    db.blast_recipients.update_many(
        {"blast_id": blast_id, "status": {"$in": list(BLAST_OPEN_STATUSES)}},
        {"$set": {"status": "cancelled", "updated_at": datetime.now(timezone.utc)}},
    )


def _recount_blast(db, user_id: str, blast_id: str) -> Optional[dict]:
    pipe = [
        {"$match": {"blast_id": blast_id}},
        {"$group": {"_id": "$status", "n": {"$sum": 1}}},
    ]
    counts = {str(row["_id"]): int(row["n"]) for row in db.blast_recipients.aggregate(pipe)}
    # Twilio create often returns "queued"/"accepted"; treat those as sent (same as Live Chat).
    sent = (
        counts.get("sent", 0)
        + counts.get("delivered", 0)
        + counts.get("read", 0)
        + counts.get("queued", 0)
        + counts.get("accepted", 0)
        + counts.get("sending", 0)
    )
    failed = counts.get("failed", 0)
    cancelled = counts.get("cancelled", 0)
    db.blast_campaigns.update_one(
        {"_id": ObjectId(blast_id)},
        {"$set": {"sent_count": sent, "failed_count": failed, "cancelled_count": cancelled}},
    )
    blast = db.blast_campaigns.find_one({"_id": ObjectId(blast_id)})
    if blast:
        _publish(
            user_id,
            "blast:updated",
            {
                "id": blast_id,
                "sent_count": blast.get("sent_count", 0),
                "failed_count": blast.get("failed_count", 0),
                "cancelled_count": blast.get("cancelled_count", 0),
                "delivered_count": blast.get("delivered_count", 0),
                "read_count": blast.get("read_count", 0),
                "undelivered_count": blast.get("undelivered_count", 0),
                "total_recipients": blast.get("total_recipients"),
                "status": blast.get("status"),
            },
        )
    return blast


def _finalize_blast(db, user_id: str, blast_id: str) -> None:
    blast = _recount_blast(db, user_id, blast_id)
    if not blast or blast.get("status") in BLAST_TERMINAL_STATUSES:
        return
    sent = int(blast.get("sent_count") or 0)
    failed = int(blast.get("failed_count") or 0)
    if sent > 0 and failed > 0:
        final_status = "partially_completed"
    elif failed > 0 and sent == 0:
        final_status = "failed"
    elif sent == 0 and failed == 0 and int(blast.get("total_recipients") or 0) == 0:
        final_status = "failed"
    else:
        final_status = "completed"
    db.blast_campaigns.update_one({"_id": ObjectId(blast_id)}, {"$set": {"status": final_status}})
    _recount_blast(db, user_id, blast_id)


def _process_blast_recipient(
    db,
    user_id: str,
    recipient: dict,
    *,
    blast: Optional[dict] = None,
    body: Optional[str],
    media_url: Optional[str],
    content_sid: Optional[str],
    content_variables: Optional[dict],
    purpose: str,
) -> None:
    updated = db.blast_recipients.find_one_and_update(
        {"_id": recipient["_id"], "status": {"$in": ["pending", "retrying"]}},
        {
            "$set": {"status": "processing", "updated_at": datetime.now(timezone.utc)},
            "$inc": {"attempt_count": 1},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        return
    from app.services.campaign_provider import classify_bulk_send_error, stored_provider

    blast = blast or {}
    camp_prov = stored_provider(blast)
    if camp_prov == "meta" and (updated.get("provider_message_id") or "").strip():
        db.blast_recipients.update_one(
            {"_id": recipient["_id"]},
            {
                "$set": {
                    "status": "sent",
                    "provider": "meta",
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
        return
    phone = updated["phone"]
    norm_phone = normalize_e164(phone) or phone

    try:
        lead = db.leads.find_one({"user_id": user_id, "phone": norm_phone})
        is_blacklisted = bool(db.blacklist.find_one({"user_id": user_id, "phone": norm_phone}))
        elig = get_whatsapp_send_eligibility(
            lead=lead or {"phone": norm_phone, "blacklisted": is_blacklisted},
            phone=norm_phone,
            purpose=purpose,
            has_template=bool(content_sid) or camp_prov == "meta",
            has_media=bool(media_url) and not content_sid and camp_prov != "meta",
            blacklisted=is_blacklisted,
            provider=camp_prov,
        )
        if not elig.allowed:
            try:
                from app.observability.metrics import inc_policy_blocked

                inc_policy_blocked(elig.reason_code)
            except Exception:
                pass
            raise RuntimeError(elig.safe_message)

        if not acquire_send_permit(user_id=user_id, priority="bulk"):
            # Give the slot back to the pool for the next batch pass.
            db.blast_recipients.update_one(
                {"_id": recipient["_id"]},
                {
                    "$set": {"status": "pending", "updated_at": datetime.now(timezone.utc)},
                    "$inc": {"attempt_count": -1},
                },
            )
            return

        try:
            if camp_prov not in ("twilio", "meta"):
                raise RuntimeError(f"Unknown WhatsApp provider: {camp_prov}")
            if camp_prov == "meta":
                from app.services.meta_templates import MetaTemplateError, build_graph_components, is_meta_template_sendable

                tid = blast.get("template_id")
                tmpl = None
                if tid and ObjectId.is_valid(str(tid)):
                    tmpl = db.templates.find_one(
                        {"_id": ObjectId(str(tid)), "user_id": user_id, "provider": "meta"}
                    )
                if not tmpl or not is_meta_template_sendable(tmpl):
                    raise RuntimeError("This Meta template cannot be sent (not approved or not supported).")
                try:
                    components = build_graph_components(
                        template=tmpl,
                        content_variables=content_variables or blast.get("content_variables"),
                    )
                except MetaTemplateError as exc:
                    raise RuntimeError(str(exc)) from exc
                user = db.users.find_one({"_id": ObjectId(user_id)}) if ObjectId.is_valid(user_id) else None
                result = send_whatsapp_template(
                    provider="meta",
                    to=phone,
                    name=(tmpl.get("meta_template_name") or blast.get("meta_template_name") or ""),
                    language_code=(tmpl.get("meta_language_code") or blast.get("meta_language_code") or ""),
                    components=components,
                    user=user,
                )
                db.blast_recipients.update_one(
                    {"_id": recipient["_id"]},
                    {
                        "$set": {
                            "status": "sent",
                            "provider": "meta",
                            "provider_message_id": result.get("provider_message_id"),
                            "twilio_sid": None,
                            "message_purpose": purpose,
                            "error": None,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
            elif content_sid:
                result = twilio_service.send_whatsapp(
                    phone, content_sid=content_sid, content_variables=content_variables
                )
            else:
                resolved_media = _resolve_media_url(media_url) if media_url else None
                if not body and not resolved_media:
                    raise RuntimeError("Blast has no message body or media")
                result = twilio_service.send_whatsapp(phone, body=body, media_url=resolved_media)

            if camp_prov != "meta":
                provider_status = (result.get("status") or "").strip().lower()
                app_status = (
                    "sent"
                    if provider_status in ("", "queued", "accepted", "sending")
                    else provider_status
                )
                db.blast_recipients.update_one(
                    {"_id": recipient["_id"]},
                    {
                        "$set": {
                            "status": app_status,
                            "provider_status": result.get("status"),
                            "twilio_sid": result.get("sid"),
                            "message_purpose": purpose,
                            "error": None,
                            "updated_at": datetime.now(timezone.utc),
                        }
                    },
                )
            try:
                from app.observability.metrics import inc_outbound

                inc_outbound(ok=True)
            except Exception:
                pass
        finally:
            release_send_permit()
    except Exception as exc:
        category = classify_bulk_send_error(exc, provider=camp_prov)
        try:
            from app.observability.metrics import inc_outbound, inc_provider_failure

            inc_outbound(ok=False)
            inc_provider_failure(category)
        except Exception:
            pass
        attempts = int(updated.get("attempt_count") or 1)
        if is_retryable_category(category) and attempts <= max_retries():
            delay = compute_retry_delay_seconds(attempts)
            db.blast_recipients.update_one(
                {"_id": recipient["_id"]},
                {
                    "$set": {
                        "status": "retrying",
                        "error": str(exc)[:500],
                        "next_retry_at": datetime.now(timezone.utc) + timedelta(seconds=delay),
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            try:
                from app.observability.metrics import inc_retry_scheduled

                inc_retry_scheduled()
            except Exception:
                pass
        else:
            db.blast_recipients.update_one(
                {"_id": recipient["_id"]},
                {
                    "$set": {
                        "status": "failed",
                        "error": str(exc)[:500],
                        "updated_at": datetime.now(timezone.utc),
                    }
                },
            )
            try:
                from app.observability.metrics import inc_retry_exhausted

                if attempts > max_retries():
                    inc_retry_exhausted()
            except Exception:
                pass


def send_blast_messages(user_id: str, blast_id: str) -> None:
    """B/blast reliability parity with the campaign engine.

    Processes one batch of due recipients (pending, or retrying whose backoff
    has elapsed), re-checking eligibility and pausing/cancelling cooperatively,
    then chains itself for the next batch via the bulk queue.
    """
    db = _db()
    blast = db.blast_campaigns.find_one({"_id": ObjectId(blast_id), "user_id": user_id})
    if not blast:
        return

    status = blast.get("status")
    if status == "paused":
        return
    if status == "cancelled":
        _cancel_open_blast_recipients(db, blast_id)
        _recount_blast(db, user_id, blast_id)
        return
    if status in BLAST_TERMINAL_STATUSES:
        return
    if status != "sending":
        db.blast_campaigns.update_one({"_id": ObjectId(blast_id)}, {"$set": {"status": "sending"}})

    content_sid = blast.get("content_sid")
    content_variables = blast.get("content_variables")
    body = blast.get("message")
    media_url = blast.get("media_url")
    if body and body.strip() in ("[media]",):
        body = None
    purpose = blast.get("message_purpose") or "marketing"

    batch_size = max(1, int(getattr(settings, "BLAST_BATCH_SIZE", 25)))
    now = datetime.now(timezone.utc)
    batch = list(
        db.blast_recipients.find(
            {
                "blast_id": blast_id,
                "$or": [
                    {"status": "pending"},
                    {"status": "retrying", "next_retry_at": {"$lte": now}},
                ],
            }
        )
        .sort("created_at", 1)
        .limit(batch_size)
    )

    from app.workers.queue import get_queue

    if not batch:
        open_n = db.blast_recipients.count_documents(
            {"blast_id": blast_id, "status": {"$in": list(BLAST_OPEN_STATUSES)}}
        )
        if open_n == 0:
            _finalize_blast(db, user_id, blast_id)
        else:
            # Nothing due yet (e.g. all remaining are in retry backoff) — check again shortly.
            get_queue(settings.RQ_BULK_QUEUE_NAME).enqueue_in(
                timedelta(seconds=5), send_blast_messages, user_id, blast_id
            )
        return

    for recipient in batch:
        fresh = db.blast_campaigns.find_one({"_id": ObjectId(blast_id)})
        fresh_status = (fresh or {}).get("status")
        if fresh_status == "paused":
            return
        if fresh_status == "cancelled":
            _cancel_open_blast_recipients(db, blast_id)
            _recount_blast(db, user_id, blast_id)
            return
        _process_blast_recipient(
            db,
            user_id,
            recipient,
            blast=fresh or blast,
            body=body,
            media_url=media_url,
            content_sid=content_sid,
            content_variables=content_variables,
            purpose=purpose,
        )

    _recount_blast(db, user_id, blast_id)

    remaining = db.blast_recipients.count_documents(
        {"blast_id": blast_id, "status": {"$in": list(BLAST_OPEN_STATUSES)}}
    )
    if remaining > 0:
        get_queue(settings.RQ_BULK_QUEUE_NAME).enqueue_in(
            timedelta(milliseconds=300), send_blast_messages, user_id, blast_id
        )
    else:
        _finalize_blast(db, user_id, blast_id)
