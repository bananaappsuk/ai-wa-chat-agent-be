"""Missed-reply recovery — no customer is left without an answer.

Customers can go unanswered when the AI can't reply (OpenAI key revoked, credits run out, our own
quota), when the server was down (Twilio's webhook got no answer and never retries), or when a
person paused AI / took over a chat and forgot about it.

`scan_missed_replies` (every couple of minutes) finds chats whose latest real message is from the
customer and has waited long enough, marks them on the contact (`missed_reply` — Live Chat shows
"Waiting"), and:
  * AI healthy        → one catch-up reply that apologises for the delay and answers everything
                        pending (never twice — see generate_and_send_ai_reply's guards);
  * AI down           → no retries; one "AI replies are down — N waiting" alert per tenant per hour;
  * paused/takeover   → waits; after AI_AUTO_HANDBACK_HOURS with no team reply, AI takes over;
  * past the window   → WhatsApp only allows templates: flagged for a person.

`reconcile_twilio_inbound` compares Twilio's message log with ours and replays any inbound message
we never received (server down) through our own webhook, so it is stored, routed and answered
like any other. Both are sync (RQ worker / scheduler) and never raise.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx
from bson import ObjectId

from app.config import settings

logger = logging.getLogger(__name__)

_FAILED = ["failed", "undelivered", "canceled", "cancelled"]
_HEALTH_KEY = "ai:health"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _db():
    from app.workers.tasks import _db as worker_db

    return worker_db()


def _redis():
    try:
        from app.workers.queue import get_redis

        return get_redis()
    except Exception:
        return None


def _probe_openai() -> tuple[bool, Optional[str]]:
    """One tiny real completion: catches revoked keys AND exhausted credits (a model list
    still works when credits are gone)."""
    key = (settings.OPENAI_API_KEY or "").strip()
    if not key:
        return False, "not_configured"
    try:
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": settings.AI_ROUTER_MODEL or "gpt-4o-mini", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
            timeout=15,
        )
    except Exception:
        return False, "network"
    if r.status_code == 200:
        return True, None
    code = ""
    try:
        code = ((r.json() or {}).get("error") or {}).get("code") or ""
    except Exception:
        pass
    if r.status_code == 401:
        return False, "authentication"
    if r.status_code == 429:
        return False, "credits_exhausted" if code == "insufficient_quota" else "rate_limited"
    if r.status_code >= 500:
        return False, "provider_down"
    return False, f"http_{r.status_code}"


def ai_health(*, refresh: bool = False) -> tuple[bool, Optional[str]]:
    """Is OpenAI usable right now? Cached for a minute so scans don't probe every time."""
    r = _redis()
    if r is not None and not refresh:
        try:
            cached = r.get(_HEALTH_KEY)
            if cached:
                val = cached.decode() if isinstance(cached, bytes) else str(cached)
                return (val == "ok"), (None if val == "ok" else val)
        except Exception:
            pass
    ok, reason = _probe_openai()
    if r is not None:
        try:
            r.set(_HEALTH_KEY, "ok" if ok else (reason or "down"), ex=60)
        except Exception:
            pass
    return ok, reason


def find_unanswered(db, *, now: Optional[datetime] = None) -> list[dict]:
    """Latest real message per chat (last 48h) is the customer's and has waited long enough.
    Holding fallbacks, failed sends, welcomes, blasts and campaigns are not answers."""
    now = now or _now()
    min_wait = timedelta(seconds=max(30, int(settings.RECOVERY_MIN_WAIT_SECONDS)))
    pipeline = [
        {
            "$match": {
                "created_at": {"$gte": now - timedelta(hours=48)},
                "status": {"$nin": _FAILED},
                "is_fallback": {"$ne": True},
                "auto_welcome": {"$ne": True},
                "blast_id": None,
                "campaign_id": None,
                "message_purpose": {"$ne": "opt_out_confirmation"},
            }
        },
        {"$sort": {"created_at": -1}},
        {"$group": {"_id": "$lead_id", "last": {"$first": "$$ROOT"}}},
        {"$match": {"last.direction": "inbound", "last.created_at": {"$lte": now - min_wait}}},
    ]
    return [row["last"] for row in db.messages.aggregate(pipeline)]


def _notify(db, *, user_id: str, type_: str, title: str, message: str, resource_type: str, resource_id: str, dedupe_key: str) -> None:
    try:
        from app.services.notifications import create_notification_sync

        create_notification_sync(db, user_id=user_id, type=type_, title=title, message=message,
                                 resource_type=resource_type, resource_id=resource_id, dedupe_key=dedupe_key)
    except Exception:
        logger.debug("recovery notification failed", exc_info=True)


def _set_state(db, lead: dict, fields: dict, *, unset: bool = False) -> None:
    """Update the contact and push it to Live Chat when its waiting state changes."""
    before = (lead.get("missed_reply") or {}).get("reason"), (lead.get("missed_reply") or {}).get("trigger_id")
    db.leads.update_one({"_id": lead["_id"]}, {"$unset": {"missed_reply": ""}} if unset else {"$set": fields})
    after = (None, None) if unset else ((fields.get("missed_reply") or {}).get("reason"), (fields.get("missed_reply") or {}).get("trigger_id"))
    if before != after or "ai_paused" in fields:
        try:
            from app.workers.tasks import _publish, _serialize

            fresh = db.leads.find_one({"_id": lead["_id"]})
            if fresh:
                _publish(str(fresh.get("user_id")), "lead:updated", _serialize(fresh))
        except Exception:
            pass


def _activity(db, *, tenant_id: str, lead_id: str, event_type: str, summary: str) -> None:
    try:
        db.activity_events.insert_one({"tenant_id": tenant_id, "user_id": tenant_id, "event_type": event_type, "summary": summary,
                                       "actor_id": None, "actor_name": "system", "resource_type": "lead", "resource_id": lead_id,
                                       "metadata": {}, "created_at": _now()})
    except Exception:
        pass


def scan_missed_replies(*, enqueue_fn=None) -> dict[str, Any]:
    """Find and recover unanswered customers. `enqueue_fn` is injectable for tests."""
    if not settings.RECOVERY_ENABLED:
        return {"enabled": False}
    db = _db()
    now = _now()
    if enqueue_fn is None:
        from app.workers.queue import enqueue as enqueue_fn  # noqa: N806
    from app.workers.tasks import generate_and_send_ai_reply

    pending = find_unanswered(db, now=now)
    summary = {"waiting": 0, "replying": 0, "ai_down": 0, "paused": 0, "handed_back": 0, "window_closed": 0, "gave_up": 0, "cleared": 0}
    waiting_ids: set[str] = set()
    health: Optional[tuple[bool, Optional[str]]] = None
    down_by_tenant: dict[str, int] = {}
    users: dict[str, Optional[dict]] = {}

    for msg in pending:
        lead_id, user_id = str(msg.get("lead_id") or ""), str(msg.get("user_id") or "")
        if not ObjectId.is_valid(lead_id):
            continue
        lead = db.leads.find_one({"_id": ObjectId(lead_id), "user_id": user_id})
        if not lead or lead.get("blacklisted") or (lead.get("whatsapp_consent_status") or "") == "opted_out":
            continue
        if user_id not in users:
            users[user_id] = db.users.find_one({"_id": ObjectId(user_id)}) if ObjectId.is_valid(user_id) else None
        if ((users[user_id] or {}).get("ai_settings") or {}).get("enabled") is False:
            continue  # this tenant answers by hand
        waiting_ids.add(lead_id)
        summary["waiting"] += 1
        since = _aware(msg["created_at"])
        waited = now - since
        trigger_id = str(msg["_id"])
        prev = lead.get("missed_reply") or {}
        state = {"trigger_id": trigger_id, "since": since, "attempts": prev.get("attempts", 0) if prev.get("trigger_id") == trigger_id else 0}
        name = lead.get("name") or lead.get("phone") or "A customer"

        if waited > timedelta(hours=float(settings.RECOVERY_REPLY_WITHIN_HOURS)):
            summary["window_closed"] += 1
            _set_state(db, lead, {"missed_reply": {**state, "reason": "window_closed"}, "needs_human": True,
                                  "needs_human_reason": "missed_window_closed"})
            _notify(db, user_id=user_id, type_="needs_human", title="A customer waited too long for a reply",
                    message=f"{name} has waited over {int(waited.total_seconds() // 3600)}h. WhatsApp now only allows an approved template.",
                    resource_type="lead", resource_id=lead_id, dedupe_key=f"missed_window:{lead_id}:{trigger_id}")
            continue

        if lead.get("ai_paused") or lead.get("takeover_by"):
            handback = float(settings.AI_AUTO_HANDBACK_HOURS or 0)
            if handback > 0 and waited >= timedelta(hours=handback):
                _set_state(db, lead, {"ai_paused": False, "ai_paused_at": None, "ai_paused_by": None,
                                      "takeover_by": None, "takeover_at": None, "updated_at": now})
                _activity(db, tenant_id=user_id, lead_id=lead_id, event_type="lead.ai_auto_resume",
                          summary=f"AI took over after {handback:g}h without a team reply")
                _notify(db, user_id=user_id, type_="system", title="AI took over a waiting chat",
                        message=f"{name} waited {int(waited.total_seconds() // 60)} min with AI paused and no team reply — AI is answering now.",
                        resource_type="lead", resource_id=lead_id, dedupe_key=f"auto_handback:{lead_id}:{trigger_id}")
                summary["handed_back"] += 1
            else:
                summary["paused"] += 1
                reason = "human_takeover" if lead.get("takeover_by") else "ai_paused"
                _set_state(db, lead, {"missed_reply": {**state, "reason": reason}})
                continue

        if health is None:
            health = ai_health()
        tenant_ok = True
        try:
            from app.services.ai_quota import check_quota

            tenant_ok, _ = check_quota(user_id)
        except Exception:
            pass
        if not health[0] or not tenant_ok:
            reason = f"ai_down:{health[1]}" if not health[0] else "ai_down:quota_exceeded"
            summary["ai_down"] += 1
            down_by_tenant[user_id] = down_by_tenant.get(user_id, 0) + 1
            _set_state(db, lead, {"missed_reply": {**state, "reason": reason}})
            continue

        last_try = prev.get("last_attempt_at")
        if prev.get("trigger_id") == trigger_id and last_try and now - _aware(last_try) < timedelta(minutes=5):
            continue  # a catch-up is already on its way
        if state["attempts"] >= int(settings.RECOVERY_MAX_ATTEMPTS):
            summary["gave_up"] += 1
            _set_state(db, lead, {"missed_reply": {**state, "reason": "reply_failed"}, "needs_human": True,
                                  "needs_human_reason": "missed_reply_failed"})
            _notify(db, user_id=user_id, type_="needs_human", title="A customer is still waiting for a reply",
                    message=f"The AI couldn't answer {name} after {state['attempts']} tries — please reply.",
                    resource_type="lead", resource_id=lead_id, dedupe_key=f"missed_gave_up:{lead_id}:{trigger_id}")
            continue
        _set_state(db, lead, {"missed_reply": {**state, "reason": "replying", "attempts": state["attempts"] + 1,
                                               "last_attempt_at": now}})
        enqueue_fn(generate_and_send_ai_reply, user_id, lead_id, provider=msg.get("provider") or "twilio",
                   trigger_message_id=trigger_id, catch_up_minutes=max(1, int(waited.total_seconds() // 60)), queue="high")
        summary["replying"] += 1

    for user_id, n in down_by_tenant.items():
        _notify(db, user_id=user_id, type_="system", title="AI replies are down",
                message=(f"OpenAI problem: {(health or (False, 'quota_exceeded'))[1] or 'quota_exceeded'}. {n} customer(s) waiting — "
                         "they'll be answered automatically as soon as AI works again."),
                resource_type="ai_health", resource_id=user_id, dedupe_key=f"ai_down:{user_id}:{now:%Y%m%d%H}")

    # Chats answered since the last scan are no longer waiting.
    for lead in db.leads.find({"missed_reply": {"$exists": True}}):
        if str(lead["_id"]) not in waiting_ids:
            _set_state(db, lead, {}, unset=True)
            summary["cleared"] += 1
    if any(summary[k] for k in ("replying", "ai_down", "handed_back", "window_closed", "gave_up")):
        logger.info("missed-reply scan %s", summary)
    return summary


def _replay_webhook(params: dict[str, str]) -> bool:
    """Send a missed inbound message through our own webhook (stored, routed, answered as usual)."""
    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        logger.warning("Twilio reconcile: PUBLIC_BASE_URL not set — cannot replay")
        return False
    from twilio.request_validator import RequestValidator

    url = f"{base}/api/webhook/whatsapp"
    sig = RequestValidator(settings.TWILIO_AUTH_TOKEN or "").compute_signature(url, params)
    try:
        r = httpx.post(url, data=params, headers={"X-Twilio-Signature": sig}, timeout=30)
        return r.status_code < 400
    except Exception:
        logger.warning("Twilio reconcile replay failed", exc_info=True)
        return False


def _twilio_client():
    from twilio.rest import Client

    return Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)


def reconcile_twilio_inbound(*, lookback_minutes: Optional[int] = None, replay_fn=None, client=None) -> dict[str, Any]:
    """Compare Twilio's inbound log with ours; replay anything we never received."""
    if not settings.TWILIO_INBOUND_RECONCILE_ENABLED or not settings.TWILIO_ACCOUNT_SID:
        return {"enabled": False}
    db = _db()
    replay_fn = replay_fn or _replay_webhook
    since = _now() - timedelta(minutes=int(lookback_minutes or settings.TWILIO_INBOUND_LOOKBACK_MINUTES))
    numbers = {u.get("twilio_whatsapp_to") for u in db.users.find({"twilio_whatsapp_to": {"$nin": [None, ""]}}, {"twilio_whatsapp_to": 1})}
    out = {"checked": 0, "missing": 0, "replayed": 0}
    try:
        client = client or _twilio_client()
    except Exception:
        logger.warning("Twilio reconcile: client unavailable", exc_info=True)
        return out
    for number in sorted(n for n in numbers if n):
        try:
            msgs = client.messages.list(to=f"whatsapp:{number}", date_sent_after=since, limit=500)
        except Exception:
            logger.warning("Twilio reconcile: list failed for …%s", str(number)[-4:], exc_info=True)
            continue
        for m in sorted(msgs, key=lambda x: x.date_sent or since):
            if not str(getattr(m, "direction", "")).startswith("inbound"):
                continue
            out["checked"] += 1
            if db.messages.find_one({"$or": [{"provider_message_id": m.sid}, {"twilio_sid": m.sid}]}, {"_id": 1}):
                continue
            out["missing"] += 1
            params = {"MessageSid": m.sid, "SmsMessageSid": m.sid, "AccountSid": settings.TWILIO_ACCOUNT_SID or "",
                      "From": m.from_, "To": m.to, "Body": m.body or "", "NumMedia": str(int(m.num_media or 0))}
            if int(m.num_media or 0) > 0:
                try:
                    for i, media in enumerate(m.media.list(limit=10)):
                        params[f"MediaUrl{i}"] = f"https://api.twilio.com{str(media.uri).replace('.json', '')}"
                        params[f"MediaContentType{i}"] = media.content_type or ""
                except Exception:
                    pass
            if replay_fn(params):
                out["replayed"] += 1
    if out["missing"]:
        logger.info("Twilio inbound reconcile %s", out)
    return out
