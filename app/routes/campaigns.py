"""Campaign CRUD + sending engine lifecycle + analytics. Blast routes preserved."""
from __future__ import annotations

from datetime import timedelta
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.campaign import (
    EDITABLE_STATUSES,
    TERMINAL_STATUSES,
    AiPreviewRequest,
    BlastCreate,
    CampaignCreate,
    CampaignUpdate,
    campaign_ai_defaults,
    empty_campaign_counters,
)
from app.models.common import serialize, utcnow
from app.services.campaign_service import (
    build_recipient_rows,
    compute_rates,
    parse_scheduled_at,
    progress_percentage,
    recount_campaign_fields,
)
from app.services.twilio_service import to_whatsapp
from app.services.ws_manager import ws_manager
from app.workers.queue import enqueue
from app.workers import tasks
from app.workers import campaign_tasks

router = APIRouter(tags=["campaigns"])


def _summary(doc: dict) -> dict:
    out = serialize(doc)
    out["progress_percentage"] = progress_percentage(doc)
    # Lightweight list fields
    return {
        "id": out.get("id"),
        "name": out.get("name"),
        "status": out.get("status"),
        "content_mode": out.get("content_mode") or "template",
        "agent_id": out.get("agent_id"),
        "campaign_subject": out.get("campaign_subject"),
        "campaign_goal": out.get("campaign_goal"),
        "ai_context_mode": out.get("ai_context_mode") or "campaign_only",
        "delivery_scope": out.get("delivery_scope") or "all_eligible_recipients",
        "knowledge_scope": out.get("knowledge_scope") or "none",
        "ai_generation_status": out.get("ai_generation_status") or "idle",
        "total_recipients": out.get("total_recipients", 0),
        "sent_count": out.get("sent_count", 0),
        "delivered_count": out.get("delivered_count", 0),
        "read_count": out.get("read_count", 0),
        "failed_count": out.get("failed_count", 0),
        "replied_count": out.get("replied_count", 0),
        "skipped_count": out.get("skipped_count", 0),
        "cancelled_count": out.get("cancelled_count", 0),
        "ai_ready_count": out.get("ai_ready_count", 0),
        "ai_review_count": out.get("ai_review_count", 0),
        "ai_failed_count": out.get("ai_failed_count", 0),
        "progress_percentage": out.get("progress_percentage", 0),
        "scheduled_at": out.get("scheduled_at"),
        "started_at": out.get("started_at"),
        "completed_at": out.get("completed_at"),
        "created_at": out.get("created_at"),
        "updated_at": out.get("updated_at"),
        "template_id": out.get("template_id"),
        "fallback_template_id": out.get("fallback_template_id"),
        "message": out.get("message"),
        "recipient_source": out.get("recipient_source"),
        "agent_snapshot": (
            {
                "id": (out.get("agent_snapshot") or {}).get("id"),
                "name": (out.get("agent_snapshot") or {}).get("name"),
                "kind": (out.get("agent_snapshot") or {}).get("kind"),
                "tone": (out.get("agent_snapshot") or {}).get("tone"),
            }
            if out.get("agent_snapshot")
            else None
        ),
    }


async def _apply_ai_campaign_fields(
    user_id: str,
    payload: CampaignCreate | CampaignUpdate,
    *,
    existing: Optional[dict] = None,
) -> dict:
    """Validate and build AI-related campaign fields for create/update."""
    from app.security.permissions import require_permission
    from app.services.ai_campaign import (
        get_campaign_agent_async,
        is_ai_campaign,
        resolve_context_flags,
        resolve_campaign_knowledge,
    )

    data = payload.model_dump(exclude_unset=True) if isinstance(payload, CampaignUpdate) else payload.model_dump()
    mode = (data.get("content_mode") or (existing or {}).get("content_mode") or "template").strip().lower()
    fields = {}
    ai_keys = [
        "content_mode",
        "agent_id",
        "campaign_subject",
        "campaign_goal",
        "campaign_instructions",
        "campaign_language",
        "campaign_tone_override",
        "review_mode",
        "preview_count",
        "fallback_template_id",
        "allow_freeform_inside_window",
        "personalise_template_variables",
        "max_ai_output_tokens",
        "ai_temperature_override",
        "require_approval_before_start",
        "on_ai_failure",
        "on_moderation_block",
        "on_low_confidence",
        "on_quota_exceeded",
        "on_window_closed_before_send",
        "ai_context_mode",
        "include_lead_profile",
        "include_conversation_summary",
        "include_recent_messages",
        "recent_message_limit",
        "knowledge_scope",
        "campaign_knowledge_text",
        "campaign_knowledge_source_ids",
        "required_topics",
        "prohibited_topics",
        "delivery_scope",
    ]
    for k in ai_keys:
        if k in data:
            fields[k] = data[k]

    if mode != "ai_agent" and fields.get("content_mode", mode) != "ai_agent":
        fields.setdefault("content_mode", "template")
        return fields

    fields["content_mode"] = "ai_agent"
    agent_id = (fields.get("agent_id") or (existing or {}).get("agent_id") or "").strip()
    goal = (fields.get("campaign_goal") or (existing or {}).get("campaign_goal") or "").strip()
    name = (fields.get("name") or (existing or {}).get("name") or data.get("name") or "").strip()
    subject = (
        (fields.get("campaign_subject") or (existing or {}).get("campaign_subject") or "").strip()
        or name
        or goal[:80]
    )
    if not agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for AI Agent campaigns")
    if not goal:
        raise HTTPException(status_code=400, detail="campaign_goal is required for AI Agent campaigns")
    if len(goal) < 8:
        raise HTTPException(status_code=400, detail="campaign_goal is too short — describe what the agent should promote")
    vague = {
        "personalised whatsapp outreach to opted-in leads",
        "personalized whatsapp outreach to opted-in leads",
        "personalised outreach",
        "personalized outreach",
    }
    if goal.lower() in vague:
        raise HTTPException(
            status_code=400,
            detail="campaign_goal is too vague — name the specific offer or topic",
        )
    fields["campaign_subject"] = subject[:200]
    fields["campaign_goal"] = goal

    try:
        agent = await get_campaign_agent_async(get_db(), user_id=user_id, agent_id=agent_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    review_mode = fields.get("review_mode") or (existing or {}).get("review_mode") or "sample_review"
    if review_mode == "no_manual_review":
        fields["require_approval_before_start"] = False

    # Context flags — never silently enable Live Chat / summary
    merged_for_flags = {**(existing or {}), **fields}
    if "ai_context_mode" not in fields and not (existing or {}).get("ai_context_mode"):
        fields["ai_context_mode"] = "campaign_only"
    flags = resolve_context_flags(merged_for_flags)
    fields.update(flags)

    # Knowledge scope defaults
    if "knowledge_scope" not in fields and not (existing or {}).get("knowledge_scope"):
        fields["knowledge_scope"] = "none"
    scope_kb = (fields.get("knowledge_scope") or (existing or {}).get("knowledge_scope") or "none").strip().lower()
    fields["knowledge_scope"] = scope_kb
    if scope_kb == "none":
        fields["campaign_knowledge_text"] = fields.get("campaign_knowledge_text") or None
        fields["campaign_knowledge_source_ids"] = fields.get("campaign_knowledge_source_ids") or []
        fields["knowledge_snapshot"] = {"scope": "none", "chars": 0}
    else:
        from app.workers.campaign_tasks import _db as sync_db_fn

        text, sources, snap = resolve_campaign_knowledge(
            sync_db_fn(),
            user_id=user_id,
            campaign={**(existing or {}), **fields},
            agent=agent,
        )
        fields["knowledge_snapshot"] = {
            **(snap or {}),
            "sources": sources,
            "text": (text or "")[:8000] or None,
            "preview_chars": min(200, len(text or "")),
        }
        if not text:
            # Selected but agent has no KB — still allow generative goal-only send
            fields["knowledge_scope"] = "selected"
            fields["knowledge_snapshot"] = {
                "scope": "selected",
                "chars": 0,
                "sources": [],
                "text": None,
                "warning": "agent_knowledge_empty",
            }

    # Delivery scope — default all_eligible_recipients for new Agent campaigns; never auto-pick a template
    delivery = (
        fields.get("delivery_scope")
        or (existing or {}).get("delivery_scope")
        or "all_eligible_recipients"
    )
    delivery = str(delivery).strip().lower()
    if delivery not in ("open_window_only", "all_eligible_recipients", "template_only"):
        raise HTTPException(status_code=400, detail="Invalid delivery_scope")
    fields["delivery_scope"] = delivery
    if delivery == "open_window_only":
        fields["allow_freeform_inside_window"] = True
        fields["on_window_closed_before_send"] = "skip"
    elif delivery == "all_eligible_recipients":
        fields["allow_freeform_inside_window"] = True
        fields["on_window_closed_before_send"] = "use_static_template"
    elif delivery == "template_only":
        fields["allow_freeform_inside_window"] = False
        fields["on_window_closed_before_send"] = "use_static_template"

    # Template only when delivery scope requires it — never auto-select
    fb_tid = fields.get("fallback_template_id") if "fallback_template_id" in fields else None
    if fb_tid is None and delivery != "open_window_only":
        # Only reuse existing fallback; do not invent from unrelated template_id on open_window
        fb_tid = (existing or {}).get("fallback_template_id")
        if not fb_tid and "fallback_template_id" in data:
            fb_tid = data.get("fallback_template_id")
        if not fb_tid and "template_id" in data:
            fb_tid = data.get("template_id")
    elif fb_tid is None and "fallback_template_id" in data:
        fb_tid = data.get("fallback_template_id")
    fb_tid = (fb_tid or "").strip() or None

    if delivery in ("all_eligible_recipients", "template_only") and not fb_tid:
        raise HTTPException(
            status_code=400,
            detail="An approved WhatsApp template is required for recipients whose 24-hour window is closed",
        )

    if delivery == "open_window_only":
        # Clear template requirement — do not force template onto open-window campaigns
        if "fallback_template_id" in fields and not fb_tid:
            fields["fallback_template_id"] = None
            fields["fallback_template_content_sid"] = None
        elif fb_tid:
            # Optional template ignored for open_window_only unless user explicitly set it for future use
            tid, csid = await _resolve_template(user_id, fb_tid, None)
            fields["fallback_template_id"] = tid
            fields["fallback_template_content_sid"] = csid
        else:
            fields["fallback_template_id"] = None
            fields["fallback_template_content_sid"] = None
            # Do not copy empty template into primary template fields
            if "template_id" not in fields:
                fields["template_id"] = None
                fields["content_sid"] = None
    elif fb_tid:
        tid, csid = await _resolve_template(user_id, fb_tid, None)
        fields["fallback_template_id"] = tid
        fields["fallback_template_content_sid"] = csid
        fields["template_id"] = tid
        fields["content_sid"] = csid
    else:
        fields["fallback_template_id"] = None
        fields["fallback_template_content_sid"] = None

    topics_req = fields.get("required_topics")
    if topics_req is None and existing:
        topics_req = existing.get("required_topics")
    topics_pro = fields.get("prohibited_topics")
    if topics_pro is None and existing:
        topics_pro = existing.get("prohibited_topics")
    if topics_req is not None and len(topics_req) > 20:
        raise HTTPException(status_code=400, detail="Too many required_topics (max 20)")
    if topics_pro is not None and len(topics_pro) > 20:
        raise HTTPException(status_code=400, detail="Too many prohibited_topics (max 20)")

    _ = is_ai_campaign
    _ = require_permission
    return fields


async def _get_owned(cid: str, user_id: str) -> dict:
    if not ObjectId.is_valid(cid):
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db().campaigns.find_one({"_id": ObjectId(cid), "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return doc


async def _resolve_template(user_id: str, template_id: Optional[str], media_url: Optional[str]):
    content_sid = None
    tid = (template_id or "").strip() or None
    if tid:
        if media_url:
            raise HTTPException(status_code=400, detail="Cannot attach media to template campaigns")
        from app.routes.templates import get_approved_template

        tmpl = await get_approved_template(user_id, tid)
        content_sid = tmpl["content_sid"]
        tid = str(tmpl["_id"])
    return tid, content_sid


async def _absolute_media(media_url: Optional[str]) -> Optional[str]:
    if not media_url:
        return None
    url = media_url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    from app.config import settings

    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=400,
            detail="PUBLIC_BASE_URL is required to send campaign media",
        )
    if not url.startswith("/"):
        url = "/" + url
    return f"{base}{url}"


@router.get("/campaigns")
async def list_campaigns(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().campaigns.find({"user_id": str(user["_id"])}).sort("created_at", -1)
    return [_summary(d) async for d in cur]


@router.get("/campaigns/{cid}")
async def get_campaign(cid: str, user: dict = Depends(current_user)) -> dict:
    doc = await _get_owned(cid, str(user["_id"]))
    out = serialize(doc)
    out["progress_percentage"] = progress_percentage(doc)
    out["rates"] = compute_rates(doc)
    return out


@router.post("/campaigns", status_code=201)
async def create_campaign(payload: CampaignCreate, user: dict = Depends(current_user)) -> dict:
    from app.config import settings
    from app.security.permissions import require_permission
    from app.security.rate_limit import rate_limit_campaign
    from app.security.validation import limit_list, limit_template_variables

    user_id = str(user["_id"])
    require_permission(user, "campaigns.create")
    rate_limit_campaign(user_id)
    db = get_db()
    max_r = int(settings.CAMPAIGN_MAX_RECIPIENTS_PER_REQUEST)
    lead_ids = limit_list(payload.lead_ids or [], max_items=max_r, field="lead_ids")
    phones = limit_list(payload.recipients or [], max_items=max_r, field="recipients")
    if payload.content_variables is not None:
        payload.content_variables = limit_template_variables(payload.content_variables)
    if not lead_ids and not phones:
        raise HTTPException(status_code=400, detail="Provide at least one recipient (lead_ids or recipients)")

    if (payload.review_mode or "") == "no_manual_review":
        require_permission(user, "campaigns.use_no_review")

    template_id, content_sid = await _resolve_template(user_id, payload.template_id, payload.media_url)
    media_url = await _absolute_media(payload.media_url) if not content_sid else None
    scheduled = parse_scheduled_at(payload.scheduled_at)
    now = utcnow()
    if scheduled and scheduled > now:
        status = "scheduled"
    else:
        status = "draft"
        scheduled = scheduled  # may be past → treat as draft until start

    ai_fields = await _apply_ai_campaign_fields(user_id, payload)
    # Template mode may still use primary template; AI mode may set fallback into template fields
    if ai_fields.get("template_id") and not template_id:
        template_id = ai_fields.get("template_id")
        content_sid = ai_fields.get("content_sid")

    doc = {
        "user_id": user_id,
        "name": payload.name.strip(),
        "description": (payload.description or "").strip() or None,
        "message": (payload.message or "").strip() or None,
        "template_id": template_id,
        "content_sid": content_sid,
        "content_variables": payload.content_variables,
        "media_url": media_url,
        "media_content_type": payload.media_content_type,
        "recipient_source": payload.recipient_source or "manual",
        "status": status,
        "scheduled_at": scheduled,
        **empty_campaign_counters(),
        **campaign_ai_defaults(),
        **ai_fields,
        "created_at": now,
        "updated_at": now,
    }
    res = await db.campaigns.insert_one(doc)
    campaign_id = str(res.inserted_id)
    doc["_id"] = res.inserted_id

    rows, _skipped = await build_recipient_rows(
        db,
        user_id=user_id,
        campaign_id=campaign_id,
        lead_ids=lead_ids,
        phones=phones,
    )
    if not rows:
        await db.campaigns.delete_one({"_id": res.inserted_id})
        raise HTTPException(status_code=400, detail="No valid recipients after validation")

    try:
        await db.campaign_recipients.insert_many(rows, ordered=False)
    except Exception:
        # unique phone conflicts — ignore duplicates
        for row in rows:
            try:
                await db.campaign_recipients.insert_one(row)
            except Exception:
                pass

    # Recount including skipped inserted as skipped
    pipe = await db.campaign_recipients.aggregate(
        [{"$match": {"campaign_id": campaign_id}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    ).to_list(50)
    counts = {str(r["_id"]): int(r["n"]) for r in pipe}
    total = sum(counts.values())
    fields = recount_campaign_fields(counts, total)
    await db.campaigns.update_one({"_id": res.inserted_id}, {"$set": fields})
    fresh = await db.campaigns.find_one({"_id": res.inserted_id})
    await ws_manager.push(user_id, "campaign:created", serialize(fresh))
    return serialize(fresh)


@router.patch("/campaigns/{cid}")
async def update_campaign(cid: str, payload: CampaignUpdate, user: dict = Depends(current_user)) -> dict:
    from app.security.permissions import require_permission

    user_id = str(user["_id"])
    require_permission(user, "campaigns.manage")
    doc = await _get_owned(cid, user_id)
    if doc.get("status") not in EDITABLE_STATUSES:
        raise HTTPException(status_code=400, detail="Campaign can only be edited before sending begins")

    if payload.review_mode == "no_manual_review":
        require_permission(user, "campaigns.use_no_review")

    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    update.pop("status", None)  # engine owns status
    # Strip AI keys — re-applied via helper for validation
    for k in list(update.keys()):
        if k in (
            "content_mode",
            "agent_id",
            "campaign_subject",
            "campaign_goal",
            "campaign_instructions",
            "campaign_language",
            "campaign_tone_override",
            "review_mode",
            "preview_count",
            "fallback_template_id",
            "allow_freeform_inside_window",
            "personalise_template_variables",
            "max_ai_output_tokens",
            "ai_temperature_override",
            "require_approval_before_start",
            "on_ai_failure",
            "on_moderation_block",
            "on_low_confidence",
            "on_quota_exceeded",
            "on_window_closed_before_send",
            "ai_context_mode",
            "include_lead_profile",
            "include_conversation_summary",
            "include_recent_messages",
            "recent_message_limit",
            "knowledge_scope",
            "campaign_knowledge_text",
            "campaign_knowledge_source_ids",
            "required_topics",
            "prohibited_topics",
            "delivery_scope",
        ):
            update.pop(k, None)

    ai_fields = await _apply_ai_campaign_fields(user_id, payload, existing=doc)
    update.update(ai_fields)

    if "template_id" in update or "media_url" in update or "fallback_template_id" in ai_fields:
        tid, csid = await _resolve_template(
            user_id,
            update.get("template_id", doc.get("template_id")),
            update.get("media_url", doc.get("media_url")),
        )
        if "template_id" in payload.model_dump(exclude_unset=True) or tid:
            update["template_id"] = tid
            update["content_sid"] = csid
        if csid:
            update["media_url"] = None
        elif "media_url" in update:
            update["media_url"] = await _absolute_media(update.get("media_url"))

    if "scheduled_at" in update:
        scheduled = parse_scheduled_at(update.get("scheduled_at"))
        update["scheduled_at"] = scheduled
        now = utcnow()
        if scheduled and scheduled > now:
            update["status"] = "scheduled"
        else:
            update["status"] = "draft"

    replace_recipients = "lead_ids" in payload.model_dump(exclude_unset=True) or "recipients" in payload.model_dump(
        exclude_unset=True
    )
    update["updated_at"] = utcnow()
    await get_db().campaigns.update_one({"_id": ObjectId(cid), "user_id": user_id}, {"$set": update})

    if replace_recipients:
        lead_ids = payload.lead_ids if payload.lead_ids is not None else []
        phones = payload.recipients if payload.recipients is not None else []
        if not lead_ids and not phones:
            raise HTTPException(status_code=400, detail="Provide at least one recipient")
        await get_db().campaign_recipients.delete_many({"campaign_id": cid})
        rows, _ = await build_recipient_rows(
            get_db(), user_id=user_id, campaign_id=cid, lead_ids=lead_ids, phones=phones
        )
        if not rows:
            raise HTTPException(status_code=400, detail="No valid recipients after validation")
        await get_db().campaign_recipients.insert_many(rows)
        pipe = await get_db().campaign_recipients.aggregate(
            [{"$match": {"campaign_id": cid}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
        ).to_list(50)
        counts = {str(r["_id"]): int(r["n"]) for r in pipe}
        fields = recount_campaign_fields(counts, sum(counts.values()))
        await get_db().campaigns.update_one({"_id": ObjectId(cid)}, {"$set": fields})

    fresh = await get_db().campaigns.find_one({"_id": ObjectId(cid)})
    await ws_manager.push(user_id, "campaign:updated", serialize(fresh))
    return serialize(fresh)


@router.delete("/campaigns/{cid}", status_code=204)
async def delete_campaign(cid: str, user: dict = Depends(current_user)) -> Response:
    user_id = str(user["_id"])
    doc = await _get_owned(cid, user_id)
    status = doc.get("status") or "draft"
    if status in ("queued", "running", "paused"):
        raise HTTPException(
            status_code=400,
            detail="Cancel or pause/finish the campaign before deleting",
        )
    # Allow delete for draft/scheduled and finished campaigns (completed/failed/cancelled/etc.)
    await get_db().campaigns.delete_one({"_id": ObjectId(cid), "user_id": user_id})
    await get_db().campaign_recipients.delete_many({"campaign_id": cid, "user_id": user_id})
    return Response(status_code=204)


@router.post("/campaigns/{cid}/start")
async def start_campaign(
    cid: str,
    user: dict = Depends(current_user),
    confirm_marketing: bool = False,
) -> dict:
    from app.middleware.security import get_request_id
    from app.security.audit import audit
    from app.security.permissions import require_permission
    from app.security.rate_limit import rate_limit_campaign
    from app.services.ai_campaign import (
        delivery_scope as resolve_delivery_scope,
        estimate_campaign_ai_cost,
        get_campaign_agent_async,
        is_ai_campaign,
        resolve_fallback_template,
        select_content_path,
        snapshot_agent,
    )
    from app.services.throughput import assert_bulk_enqueue_allowed

    user_id = str(user["_id"])
    require_permission(user, "campaigns.start")
    rate_limit_campaign(user_id)
    doc = await _get_owned(cid, user_id)
    if doc.get("status") not in ("draft", "scheduled", "queued"):
        raise HTTPException(status_code=400, detail="Only draft or scheduled campaigns can start")
    if int(doc.get("total_recipients") or 0) <= 0:
        raise HTTPException(status_code=400, detail="Campaign has no recipients")
    if not confirm_marketing:
        raise HTTPException(
            status_code=400,
            detail="Marketing campaigns require confirm_marketing=true before launch",
        )

    # Block launch when nobody can be sent (avoids "Completed" with 0 sent).
    pending = await get_db().campaign_recipients.count_documents(
        {"campaign_id": cid, "user_id": user_id, "status": {"$in": ["pending", "queued"]}}
    )
    if pending <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "No eligible recipients to send. Campaigns require WhatsApp opt-in. "
                "Open each lead in Live Chat → Lead Info → Opt in, then create/start again."
            ),
        )

    # Also re-check live eligibility so consent_required leads aren't silently skipped.
    from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility

    tid, sid = resolve_fallback_template(doc) if is_ai_campaign(doc) else (
        doc.get("template_id"),
        doc.get("content_sid"),
    )
    has_template = bool(sid or tid or doc.get("content_sid") or doc.get("template_id"))
    eligible = 0
    freeform_n = 0
    tmpl_ai_n = 0
    static_n = 0
    async for r in get_db().campaign_recipients.find(
        {"campaign_id": cid, "user_id": user_id, "status": {"$in": ["pending", "queued"]}}
    ):
        lead = None
        lid = r.get("lead_id")
        if lid and ObjectId.is_valid(str(lid)):
            lead = await get_db().leads.find_one({"_id": ObjectId(str(lid)), "user_id": user_id})
        if not lead and r.get("phone"):
            lead = await get_db().leads.find_one({"user_id": user_id, "phone": r["phone"]})
        if is_ai_campaign(doc):
            path = select_content_path(campaign=doc, lead=lead, phone=r.get("phone"))
            if path.path == "ineligible":
                continue
            eligible += 1
            if path.path == "ai_freeform":
                freeform_n += 1
            elif path.path == "ai_template_variables":
                tmpl_ai_n += 1
            else:
                static_n += 1
        else:
            elig = get_whatsapp_send_eligibility(
                lead=lead or {"phone": r.get("phone")},
                phone=r.get("phone"),
                purpose="campaign",
                has_template=has_template,
            )
            if elig.allowed:
                eligible += 1
    if eligible <= 0:
        raise HTTPException(
            status_code=400,
            detail=(
                "No eligible recipients (consent_required). "
                "Opt in each lead under Live Chat → Lead Info → Opt in, then retry."
            ),
        )

    now = utcnow()
    extra_set: dict = {
        "status": "queued",
        "updated_at": now,
        "last_error": None,
        "message_purpose": "campaign",
    }

    if is_ai_campaign(doc):
        try:
            agent = await get_campaign_agent_async(
                get_db(), user_id=user_id, agent_id=str(doc.get("agent_id") or "")
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        scope = resolve_delivery_scope(doc)
        if scope in ("all_eligible_recipients", "template_only") and not has_template:
            raise HTTPException(
                status_code=400,
                detail="An approved WhatsApp template is required for this delivery scope",
            )
        if scope == "open_window_only" and freeform_n <= 0 and eligible <= 0:
            raise HTTPException(
                status_code=400,
                detail="No recipients with an open WhatsApp window — closed-window leads are skipped for this campaign",
            )
        review_mode = doc.get("review_mode") or "sample_review"
        require_approval = bool(doc.get("require_approval_before_start", True))
        if review_mode == "no_manual_review":
            require_permission(user, "campaigns.use_no_review")
            require_approval = False
        if require_approval and review_mode == "full_review":
            # Only recipients that can actually be sent must be generated + approved.
            # Ineligible (skipped closed window, opted out, etc.) must not block launch.
            unapproved = 0
            ready = 0
            async for r in get_db().campaign_recipients.find(
                {"campaign_id": cid, "user_id": user_id, "status": {"$in": ["pending", "queued"]}}
            ):
                lead = None
                lid = r.get("lead_id")
                if lid and ObjectId.is_valid(str(lid)):
                    lead = await get_db().leads.find_one({"_id": ObjectId(str(lid)), "user_id": user_id})
                if not lead and r.get("phone"):
                    lead = await get_db().leads.find_one({"user_id": user_id, "phone": r["phone"]})
                path = select_content_path(campaign=doc, lead=lead, phone=r.get("phone"))
                if path.path == "ineligible":
                    continue
                if r.get("ai_approved") is True and (r.get("ai_generation_status") or "") == "ready":
                    ready += 1
                else:
                    unapproved += 1
            if ready <= 0 or unapproved > 0:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Full review incomplete — generate and approve all eligible recipients before start "
                        f"(ready={ready}, still_unapproved={unapproved}). "
                        "Use Sample review if you only need to approve a few messages."
                    ),
                )
        if require_approval and review_mode == "sample_review" and not doc.get("approved_at"):
            raise HTTPException(
                status_code=400,
                detail="Approve AI sample content before starting this campaign",
            )

        snap = snapshot_agent(agent)
        # Strip long prompt from API-facing snapshot response later; store for generation
        extra_set["agent_snapshot"] = snap
        extra_set["ai_generation_status"] = "pending"
        extra_set["ai_generation_started_at"] = now
        cost_est = estimate_campaign_ai_cost(freeform_count=freeform_n, template_var_count=tmpl_ai_n)
        extra_set["ai_cost_estimate"] = {
            **cost_est,
            "eligible_recipients": eligible,
            "static_fallback_count": static_n,
            "ineligible_estimate": max(0, int(pending) - eligible),
        }

    assert_bulk_enqueue_allowed(estimated_jobs=max(1, int(pending)))

    scheduled = doc.get("scheduled_at")
    if scheduled and hasattr(scheduled, "tzinfo"):
        if scheduled > now and doc.get("status") == "scheduled":
            raise HTTPException(status_code=400, detail="Campaign is scheduled for the future")

    await get_db().campaigns.update_one({"_id": ObjectId(cid)}, {"$set": extra_set})
    if is_ai_campaign(doc):
        enqueue(campaign_tasks.start_ai_campaign_job, user_id, cid, queue="bulk")
    else:
        enqueue(campaign_tasks.start_campaign_job, user_id, cid, queue="bulk")
    fresh = await get_db().campaigns.find_one({"_id": ObjectId(cid)})
    audit("campaign.start", user_id=user_id, target_id=cid, request_id=get_request_id())
    await ws_manager.push(user_id, "campaign:updated", serialize(fresh))
    out = serialize(fresh)
    if out.get("ai_cost_estimate"):
        out["ai_cost_estimate"] = fresh.get("ai_cost_estimate")
    return out


@router.get("/campaigns/{cid}/eligibility-preview")
async def campaign_eligibility_preview(cid: str, user: dict = Depends(current_user)) -> dict:
    """Pre-launch eligibility summary for UI confirmation."""
    from app.middleware.security import get_request_id
    from app.security.audit import audit
    from app.services.ai_campaign import (
        delivery_scope as resolve_delivery_scope,
        is_ai_campaign,
        resolve_fallback_template,
        select_content_path,
    )
    from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility

    user_id = str(user["_id"])
    doc = await _get_owned(cid, user_id)
    tid, sid = resolve_fallback_template(doc) if is_ai_campaign(doc) else (
        doc.get("template_id"),
        doc.get("content_sid"),
    )
    has_template = bool(sid or tid or doc.get("content_sid") or doc.get("template_id"))
    scope = resolve_delivery_scope(doc) if is_ai_campaign(doc) else "template_only"
    counts = {
        "total_selected": 0,
        "eligible": 0,
        "eligible_ai_freeform": 0,
        "eligible_freeform_ai": 0,
        "eligible_template_fallback": 0,
        "eligible_template_ai": 0,
        "eligible_template_static": 0,
        "opted_out": 0,
        "no_consent": 0,
        "blacklisted": 0,
        "closed_window": 0,
        "skipped_closed_window": 0,
        "closed_window_missing_template": 0,
        "template_required": 0,
        "template_missing": 0,
        "template_not_approved": 0,
        "invalid_number": 0,
        "invalid_phone": 0,
        "duplicates_removed": 0,
        "other_blocked": 0,
        "agent_unavailable": 0,
        "ai_disabled": 0,
        "quota_blocked": 0,
        "needs_manual_review": 0,
        "total": 0,
        "delivery_scope": scope,
        "template_required_for_scope": scope in ("all_eligible_recipients", "template_only"),
        "samples": [],
    }
    seen: set[str] = set()
    async for r in get_db().campaign_recipients.find({"campaign_id": cid, "user_id": user_id}):
        phone = r.get("phone") or ""
        counts["total_selected"] += 1
        counts["total"] += 1
        if phone in seen:
            counts["duplicates_removed"] += 1
            continue
        seen.add(phone)
        if r.get("status") == "skipped":
            reason = (r.get("error_message") or "").lower()
            if "blacklist" in reason:
                counts["blacklisted"] += 1
            elif "consent_required" in reason or "no consent" in reason:
                counts["no_consent"] += 1
            elif "consent_blocked" in reason or "opt" in reason:
                counts["opted_out"] += 1
            elif "invalid" in reason:
                counts["invalid_number"] += 1
                counts["invalid_phone"] += 1
            else:
                counts["other_blocked"] += 1
            continue
        lead = None
        if r.get("lead_id") and ObjectId.is_valid(str(r["lead_id"])):
            lead = await get_db().leads.find_one({"_id": ObjectId(str(r["lead_id"])), "user_id": user_id})
        if not lead and phone:
            lead = await get_db().leads.find_one({"user_id": user_id, "phone": phone})

        sample = {
            "recipient_id": str(r["_id"]),
            "lead_id": r.get("lead_id"),
            "name": r.get("name"),
            "phone": phone,
        }
        if is_ai_campaign(doc):
            path = select_content_path(campaign=doc, lead=lead, phone=phone)
            sample["content_path"] = path.path
            sample["reason_code"] = path.reason_code
            sample["safe_message"] = path.safe_message
            if path.path == "ai_freeform":
                counts["eligible"] += 1
                counts["eligible_freeform_ai"] += 1
                counts["eligible_ai_freeform"] += 1
            elif path.path == "ai_template_variables":
                counts["eligible"] += 1
                counts["eligible_template_ai"] += 1
                counts["eligible_template_fallback"] += 1
            elif path.path == "template":
                counts["eligible"] += 1
                counts["eligible_template_static"] += 1
                counts["eligible_template_fallback"] += 1
            elif path.reason_code == "skipped_closed_window":
                counts["skipped_closed_window"] += 1
                counts["closed_window"] += 1
            elif path.reason_code == "closed_window_missing_template":
                counts["closed_window_missing_template"] += 1
                counts["closed_window"] += 1
                counts["template_required"] += 1
                counts["template_missing"] += 1
            elif path.reason_code == "consent_blocked":
                counts["opted_out"] += 1
            elif path.reason_code == "consent_required":
                counts["no_consent"] += 1
            elif path.reason_code == "invalid_recipient":
                counts["invalid_number"] += 1
                counts["invalid_phone"] += 1
            else:
                counts["other_blocked"] += 1
        else:
            elig = get_whatsapp_send_eligibility(
                lead=lead or {"phone": phone},
                phone=phone,
                purpose="campaign",
                has_template=has_template,
            )
            sample["content_path"] = "template" if has_template else "freeform"
            sample["reason_code"] = elig.reason_code
            sample["safe_message"] = elig.safe_message
            if elig.allowed:
                counts["eligible"] += 1
                if has_template:
                    counts["eligible_template_static"] += 1
            elif elig.reason_code == "consent_blocked":
                counts["opted_out"] += 1
            elif elig.reason_code == "consent_required":
                counts["no_consent"] += 1
            elif elig.reason_code == "window_closed":
                counts["closed_window"] += 1
                counts["template_required"] += 1
            elif elig.reason_code == "invalid_recipient":
                counts["invalid_number"] += 1
                counts["invalid_phone"] += 1
            else:
                counts["other_blocked"] += 1
        if len(counts["samples"]) < 10:
            counts["samples"].append(sample)
    # Meta WhatsApp template approval gate (closed-window / template sends)
    from app.services.whatsapp_template_approval import (
        display_status_label,
        is_whatsapp_template_sendable,
        mask_content_sid,
        normalize_whatsapp_approval_status,
        status_emoji,
    )

    tmpl_name = None
    tmpl_sid = sid
    wa_status = None
    if tid and ObjectId.is_valid(str(tid)):
        tdoc = await get_db().templates.find_one({"_id": ObjectId(str(tid)), "user_id": user_id})
        if tdoc:
            tmpl_name = tdoc.get("name")
            tmpl_sid = tdoc.get("content_sid") or tmpl_sid
            wa_status = tdoc.get("whatsapp_approval_status")
    if tmpl_sid:
        try:
            from app.services import twilio_service

            info = twilio_service.get_content_template_info(str(tmpl_sid))
            wa_status = info.get("whatsapp_status")
            tmpl_name = tmpl_name or info.get("friendly_name")
            if tid and ObjectId.is_valid(str(tid)):
                await get_db().templates.update_one(
                    {"_id": ObjectId(str(tid)), "user_id": user_id},
                    {
                        "$set": {
                            "whatsapp_approval_status": wa_status,
                            "whatsapp_category": info.get("whatsapp_category"),
                            "whatsapp_approval_checked_at": utcnow(),
                            "updated_at": utcnow(),
                        }
                    },
                )
        except Exception:
            pass
    wa_norm = normalize_whatsapp_approval_status(wa_status) if wa_status else None
    template_approved = is_whatsapp_template_sendable(wa_status) if wa_status else False
    needs_template = scope in ("all_eligible_recipients", "template_only") or (
        not is_ai_campaign(doc) and has_template
    )
    counts["template_meta"] = {
        "name": tmpl_name,
        "content_sid": tmpl_sid,
        "content_sid_masked": mask_content_sid(tmpl_sid),
        "whatsapp_approval_status": wa_norm,
        "whatsapp_approval_label": display_status_label(wa_norm) if wa_norm else None,
        "whatsapp_approval_emoji": status_emoji(wa_norm) if wa_norm else None,
        "whatsapp_sendable": template_approved,
        "warning_required": bool(needs_template and tmpl_sid and wa_norm and not template_approved),
        "warning_message": (
            "The selected WhatsApp template has not yet been approved by Meta.\n\n"
            "Recipients outside the 24-hour WhatsApp window will be skipped."
            if needs_template and tmpl_sid and wa_norm and not template_approved
            else None
        ),
    }
    if wa_norm and not template_approved:
        counts["template_not_approved"] = max(
            int(counts.get("template_not_approved") or 0),
            int(counts.get("eligible_template_fallback") or 0)
            + int(counts.get("eligible_template_static") or 0)
            + int(counts.get("closed_window") or 0),
        )
    audit("campaign.eligibility_preview", user_id=user_id, target_id=cid, request_id=get_request_id())
    return {"campaign_id": cid, **counts}


@router.post("/campaigns/{cid}/ai-preview")
async def campaign_ai_preview(
    cid: str,
    payload: AiPreviewRequest | None = None,
    user: dict = Depends(current_user),
) -> dict:
    """Generate AI previews for a small sample — does not send WhatsApp."""
    from app.middleware.security import get_request_id
    from app.security.audit import audit
    from app.security.permissions import require_permission
    from app.security.rate_limit import rate_limit_campaign
    from app.services.activity import record_activity
    from app.services.ai_campaign import (
        apply_generation_to_recipient,
        generate_campaign_content,
        get_campaign_agent_async,
        is_ai_campaign,
        recipient_ai_idempotency_key,
        resolve_context_flags,
        select_content_path,
        snapshot_agent,
    )
    from app.services.idempotency import claim_idempotency
    from app.services.notifications import create_notification

    user_id = str(user["_id"])
    require_permission(user, "campaigns.manage")
    rate_limit_campaign(user_id)
    doc = await _get_owned(cid, user_id)
    if not is_ai_campaign(doc):
        raise HTTPException(status_code=400, detail="AI preview is only available for AI Agent campaigns")
    subject = (doc.get("campaign_subject") or doc.get("name") or "").strip()
    goal = (doc.get("campaign_goal") or "").strip()
    if not goal or len(goal) < 8:
        raise HTTPException(status_code=400, detail="campaign_goal is required before preview")
    if not subject:
        subject = goal[:80]
    if doc.get("status") not in EDITABLE_STATUSES and doc.get("status") not in ("queued",):
        # allow preview while draft/scheduled
        if doc.get("status") not in ("draft", "scheduled"):
            raise HTTPException(status_code=400, detail="AI preview only before or while preparing campaign")

    payload = payload or AiPreviewRequest()
    preview_count = int(payload.preview_count or doc.get("preview_count") or 5)
    preview_count = max(1, min(25, preview_count, int(doc.get("preview_count") or 25)))
    regenerate = bool(payload.regenerate)
    ctx_flags = resolve_context_flags(doc)

    try:
        agent = await get_campaign_agent_async(get_db(), user_id=user_id, agent_id=str(doc.get("agent_id") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    snap = doc.get("agent_snapshot") or snapshot_agent(agent)

    q: dict = {"campaign_id": cid, "user_id": user_id, "status": {"$in": ["pending", "queued"]}}
    if payload.selected_lead_ids:
        q["lead_id"] = {"$in": [str(x) for x in payload.selected_lead_ids[:preview_count]]}

    recipients = await get_db().campaign_recipients.find(q).limit(preview_count).to_list(preview_count)
    previews = []
    # Sync pymongo for generation service
    from app.workers.campaign_tasks import _db as sync_db_fn

    sdb = sync_db_fn()
    for r in recipients:
        rid = str(r["_id"])
        version = int(r.get("generation_version") or 0) + (1 if regenerate else 0)
        idem = recipient_ai_idempotency_key(cid, rid, max(1, version or 1))
        if (
            not regenerate
            and r.get("ai_generation_status") in ("ready", "needs_review")
            and (r.get("generated_message") or r.get("generated_template_variables"))
        ):
            path = select_content_path(
                campaign=doc,
                lead=None,
                phone=r.get("phone"),
            )
            previews.append(
                {
                    "recipient_id": rid,
                    "lead_id": r.get("lead_id"),
                    "name": r.get("name"),
                    "content_path": r.get("content_source") or path.path,
                    "generated_message": r.get("generated_message"),
                    "generated_template_variables": r.get("generated_template_variables"),
                    "moderation_status": "ok",
                    "quality_status": "ok",
                    "warnings": r.get("ai_warnings") or [],
                    "token_usage": {
                        "input": r.get("ai_input_tokens") or 0,
                        "output": r.get("ai_output_tokens") or 0,
                    },
                    "estimated_cost": r.get("ai_estimated_cost") or 0,
                    "eligibility": path.eligibility,
                    "context_sources_used": r.get("context_sources_used") or [],
                    "knowledge_sources_used": r.get("knowledge_sources_used") or [],
                    "topic_alignment_passed": r.get("topic_alignment_passed"),
                    "prohibited_topics_found": r.get("prohibited_topics_found") or [],
                    "required_topics_missing": r.get("required_topics_missing") or [],
                    "cached": True,
                }
            )
            continue
        if not claim_idempotency(user_id, idem) and not regenerate:
            continue
        lead = None
        if r.get("lead_id") and ObjectId.is_valid(str(r["lead_id"])):
            lead = sdb.leads.find_one({"_id": ObjectId(str(r["lead_id"])), "user_id": user_id})
        path = select_content_path(campaign=doc, lead=lead, phone=r.get("phone"))
        gen = generate_campaign_content(
            sdb,
            user_id=user_id,
            campaign=doc,
            recipient=r,
            lead=lead,
            agent_snapshot=snap,
            preview=True,
        )
        fields = apply_generation_to_recipient(recipient=r, gen=gen, version_inc=regenerate)
        fields["ai_idempotency_key"] = idem
        fields["is_preview_only"] = True
        await get_db().campaign_recipients.update_one({"_id": r["_id"]}, {"$set": fields})
        previews.append(
            {
                "recipient_id": rid,
                "lead_id": r.get("lead_id"),
                "name": r.get("name"),
                "content_path": gen.content_source or path.path,
                "generated_message": gen.message,
                "generated_template_variables": gen.template_variables,
                "moderation_status": "blocked" if gen.error_category in ("moderation", "content_blocked") else "ok",
                "quality_status": "ok" if gen.ok else (gen.error_category or "failed"),
                "warnings": gen.warnings,
                "token_usage": {"input": gen.input_tokens, "output": gen.output_tokens},
                "estimated_cost": gen.estimated_cost,
                "eligibility": path.eligibility,
                "needs_manual_review": gen.needs_manual_review,
                "context_sources_used": gen.context_sources_used,
                "knowledge_sources_used": gen.knowledge_sources_used,
                "topic_alignment_passed": gen.topic_alignment_passed,
                "prohibited_topics_found": gen.prohibited_topics_found,
                "required_topics_missing": gen.required_topics_missing,
                "unrelated_topic_detected": gen.unrelated_topic_detected,
                "cached": False,
            }
        )

    await get_db().campaigns.update_one(
        {"_id": ObjectId(cid)},
        {
            "$set": {
                "ai_generation_status": "needs_review",
                "updated_at": utcnow(),
                "agent_snapshot": snap,
            }
        },
    )
    await get_db().campaign_ai_previews.insert_one(
        {
            "campaign_id": cid,
            "user_id": user_id,
            "preview_count": len(previews),
            "created_at": utcnow(),
            "regenerate": regenerate,
        }
    )
    await create_notification(
        get_db(),
        user_id=user_id,
        type="campaign_ai_preview_ready",
        title="AI campaign preview ready",
        message=f"{len(previews)} preview message(s) ready for review",
        resource_type="campaign",
        resource_id=cid,
        dedupe_key=f"camp_ai_preview:{cid}:{utcnow().strftime('%Y%m%d%H')}",
    )
    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="campaign.ai_preview",
        actor_id=user_id,
        resource_type="campaign",
        resource_id=cid,
        summary=f"Generated {len(previews)} AI preview(s)",
    )
    audit("campaign.ai_preview", user_id=user_id, target_id=cid, request_id=get_request_id())
    return {
        "campaign_id": cid,
        "previews": previews,
        "count": len(previews),
        "campaign_subject": subject,
        "ai_context_mode": ctx_flags.get("ai_context_mode"),
        "include_conversation_summary": bool(ctx_flags.get("include_conversation_summary")),
        "include_recent_messages": bool(ctx_flags.get("include_recent_messages")),
        "include_lead_profile": bool(ctx_flags.get("include_lead_profile")),
        "knowledge_scope": doc.get("knowledge_scope") or "none",
        "knowledge_sources": (doc.get("knowledge_snapshot") or {}).get("sources") or [],
        "delivery_scope": doc.get("delivery_scope") or "all_eligible_recipients",
        "fallback_template_id": doc.get("fallback_template_id"),
    }


@router.post("/campaigns/{cid}/approve-ai-content")
async def approve_ai_content(cid: str, user: dict = Depends(current_user)) -> dict:
    from app.middleware.security import get_request_id
    from app.security.audit import audit
    from app.security.permissions import require_permission
    from app.services.activity import record_activity
    from app.services.ai_campaign import is_ai_campaign

    user_id = str(user["_id"])
    require_permission(user, "campaigns.approve_ai")
    doc = await _get_owned(cid, user_id)
    if not is_ai_campaign(doc):
        raise HTTPException(status_code=400, detail="Not an AI Agent campaign")
    now = utcnow()
    await get_db().campaign_recipients.update_many(
        {
            "campaign_id": cid,
            "user_id": user_id,
            "ai_generation_status": {"$in": ["ready", "needs_review"]},
            "status": {"$in": ["pending", "queued"]},
        },
        {"$set": {"ai_approved": True, "ai_approved_at": now, "ai_generation_status": "ready", "updated_at": now}},
    )
    await get_db().campaigns.update_one(
        {"_id": ObjectId(cid)},
        {
            "$set": {
                "approved_at": now,
                "approved_by": user_id,
                "ai_generation_status": "ready",
                "updated_at": now,
            }
        },
    )
    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="campaign.ai_content_approved",
        actor_id=user_id,
        resource_type="campaign",
        resource_id=cid,
        summary="Approved AI campaign content",
    )
    audit("campaign.approve_ai", user_id=user_id, target_id=cid, request_id=get_request_id())
    fresh = await get_db().campaigns.find_one({"_id": ObjectId(cid)})
    await ws_manager.push(user_id, "campaign:updated", serialize(fresh))
    return serialize(fresh)


@router.post("/campaigns/{cid}/recipients/{rid}/approve")
async def approve_recipient_ai(cid: str, rid: str, user: dict = Depends(current_user)) -> dict:
    from app.security.permissions import require_permission
    from app.services.activity import record_activity
    from pymongo import ReturnDocument

    user_id = str(user["_id"])
    require_permission(user, "campaigns.approve_ai")
    await _get_owned(cid, user_id)
    if not ObjectId.is_valid(rid):
        raise HTTPException(status_code=404, detail="Not found")
    now = utcnow()
    doc = await get_db().campaign_recipients.find_one_and_update(
        {"_id": ObjectId(rid), "campaign_id": cid, "user_id": user_id},
        {
            "$set": {
                "ai_approved": True,
                "ai_approved_at": now,
                "ai_generation_status": "ready",
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="campaign.recipient_ai_approved",
        actor_id=user_id,
        resource_type="campaign_recipient",
        resource_id=rid,
        summary="Approved recipient AI content",
    )
    return serialize(doc)


@router.post("/campaigns/{cid}/recipients/{rid}/reject")
async def reject_recipient_ai(cid: str, rid: str, user: dict = Depends(current_user)) -> dict:
    from app.security.permissions import require_permission
    from app.services.activity import record_activity

    user_id = str(user["_id"])
    require_permission(user, "campaigns.approve_ai")
    await _get_owned(cid, user_id)
    if not ObjectId.is_valid(rid):
        raise HTTPException(status_code=404, detail="Not found")
    from pymongo import ReturnDocument

    now = utcnow()
    doc = await get_db().campaign_recipients.find_one_and_update(
        {"_id": ObjectId(rid), "campaign_id": cid, "user_id": user_id},
        {
            "$set": {
                "ai_approved": False,
                "ai_generation_status": "failed",
                "ai_generation_error_category": "rejected",
                "generated_message": None,
                "generated_template_variables": None,
                "updated_at": now,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="campaign.recipient_ai_rejected",
        actor_id=user_id,
        resource_type="campaign_recipient",
        resource_id=rid,
        summary="Rejected recipient AI content",
    )
    return serialize(doc)


@router.post("/campaigns/{cid}/recipients/{rid}/regenerate")
async def regenerate_recipient_ai(cid: str, rid: str, user: dict = Depends(current_user)) -> dict:
    from app.security.permissions import require_permission
    from app.services.activity import record_activity
    from app.services.ai_campaign import (
        apply_generation_to_recipient,
        generate_campaign_content,
        is_ai_campaign,
        recipient_ai_idempotency_key,
    )
    from app.workers.campaign_tasks import _db as sync_db_fn

    user_id = str(user["_id"])
    require_permission(user, "campaigns.approve_ai")
    camp = await _get_owned(cid, user_id)
    if not is_ai_campaign(camp):
        raise HTTPException(status_code=400, detail="Not an AI Agent campaign")
    if not ObjectId.is_valid(rid):
        raise HTTPException(status_code=404, detail="Not found")
    r = await get_db().campaign_recipients.find_one(
        {"_id": ObjectId(rid), "campaign_id": cid, "user_id": user_id}
    )
    if not r:
        raise HTTPException(status_code=404, detail="Not found")
    sdb = sync_db_fn()
    lead = None
    if r.get("lead_id") and ObjectId.is_valid(str(r["lead_id"])):
        lead = sdb.leads.find_one({"_id": ObjectId(str(r["lead_id"])), "user_id": user_id})
    gen = generate_campaign_content(
        sdb,
        user_id=user_id,
        campaign=camp,
        recipient=r,
        lead=lead,
        agent_snapshot=camp.get("agent_snapshot"),
        preview=True,
    )
    fields = apply_generation_to_recipient(recipient=r, gen=gen, version_inc=True)
    fields["ai_approved"] = False
    fields["ai_approved_at"] = None
    fields["ai_idempotency_key"] = recipient_ai_idempotency_key(
        cid, rid, int(fields.get("generation_version") or 1)
    )
    from pymongo import ReturnDocument

    doc = await get_db().campaign_recipients.find_one_and_update(
        {"_id": ObjectId(rid)},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="campaign.recipient_ai_regenerated",
        actor_id=user_id,
        resource_type="campaign_recipient",
        resource_id=rid,
        summary="Regenerated recipient AI content",
    )
    return serialize(doc)


@router.post("/campaigns/{cid}/pause")
async def pause_campaign(cid: str, user: dict = Depends(current_user)) -> dict:
    from app.middleware.security import get_request_id
    from app.security.audit import audit

    user_id = str(user["_id"])
    doc = await _get_owned(cid, user_id)
    if doc.get("status") != "running":
        raise HTTPException(status_code=400, detail="Only running campaigns can pause")
    await get_db().campaigns.update_one(
        {"_id": ObjectId(cid)},
        {"$set": {"status": "paused", "paused_at": utcnow(), "updated_at": utcnow()}},
    )
    fresh = await get_db().campaigns.find_one({"_id": ObjectId(cid)})
    audit("campaign.pause", user_id=user_id, target_id=cid, request_id=get_request_id())
    await ws_manager.push(user_id, "campaign:paused", serialize(fresh))
    return serialize(fresh)


@router.post("/campaigns/{cid}/resume")
async def resume_campaign(cid: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    doc = await _get_owned(cid, user_id)
    if doc.get("status") != "paused":
        raise HTTPException(status_code=400, detail="Only paused campaigns can resume")
    await get_db().campaigns.update_one(
        {"_id": ObjectId(cid)},
        {"$set": {"status": "running", "paused_at": None, "updated_at": utcnow()}},
    )
    # Requeue processing stuck? leave processing; queued stay queued
    await get_db().campaign_recipients.update_many(
        {"campaign_id": cid, "status": "processing"},
        {"$set": {"status": "queued", "updated_at": utcnow()}},
    )
    enqueue(campaign_tasks.process_campaign_batch, user_id, cid)
    fresh = await get_db().campaigns.find_one({"_id": ObjectId(cid)})
    await ws_manager.push(user_id, "campaign:resumed", serialize(fresh))
    return serialize(fresh)


@router.post("/campaigns/{cid}/cancel")
async def cancel_campaign(cid: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    doc = await _get_owned(cid, user_id)
    if doc.get("status") in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail="Campaign already finished")
    now = utcnow()
    await get_db().campaigns.update_one(
        {"_id": ObjectId(cid)},
        {"$set": {"status": "cancelled", "cancelled_at": now, "completed_at": now, "updated_at": now}},
    )
    await get_db().campaign_recipients.update_many(
        {"campaign_id": cid, "status": {"$in": ["pending", "queued", "retrying", "processing"]}},
        {"$set": {"status": "cancelled", "updated_at": now}},
    )
    # recount
    pipe = await get_db().campaign_recipients.aggregate(
        [{"$match": {"campaign_id": cid}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    ).to_list(50)
    counts = {str(r["_id"]): int(r["n"]) for r in pipe}
    fields = recount_campaign_fields(counts, sum(counts.values()))
    fields["status"] = "cancelled"
    await get_db().campaigns.update_one({"_id": ObjectId(cid)}, {"$set": fields})
    fresh = await get_db().campaigns.find_one({"_id": ObjectId(cid)})
    await ws_manager.push(user_id, "campaign:cancelled", serialize(fresh))
    return serialize(fresh)


@router.post("/campaigns/{cid}/retry-failed")
async def retry_failed(cid: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    doc = await _get_owned(cid, user_id)
    if doc.get("status") in ("cancelled",):
        raise HTTPException(status_code=400, detail="Cannot retry a cancelled campaign")
    now = utcnow()
    res = await get_db().campaign_recipients.update_many(
        {"campaign_id": cid, "user_id": user_id, "status": "failed"},
        {
            "$set": {
                "status": "queued",
                "error_message": None,
                "error_code": None,
                "next_retry_at": None,
                "updated_at": now,
            }
        },
    )
    if res.modified_count == 0:
        raise HTTPException(status_code=400, detail="No failed recipients to retry")
    await get_db().campaigns.update_one(
        {"_id": ObjectId(cid)},
        {"$set": {"status": "running", "completed_at": None, "updated_at": now}},
    )
    enqueue(campaign_tasks.process_campaign_batch, user_id, cid)
    fresh = await get_db().campaigns.find_one({"_id": ObjectId(cid)})
    await ws_manager.push(user_id, "campaign:updated", serialize(fresh))
    return serialize(fresh)


@router.get("/campaigns/{cid}/recipients")
async def list_campaign_recipients(
    cid: str,
    user: dict = Depends(current_user),
    status: Optional[str] = None,
    q: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort: str = Query(default="created_at"),
) -> dict:
    user_id = str(user["_id"])
    await _get_owned(cid, user_id)
    filt: dict = {"campaign_id": cid, "user_id": user_id}
    if status:
        filt["status"] = status
    if q and q.strip():
        filt["$or"] = [
            {"phone": {"$regex": q.strip(), "$options": "i"}},
            {"name": {"$regex": q.strip(), "$options": "i"}},
        ]
    sort_field = "updated_at" if sort == "updated_at" else "created_at"
    total = await get_db().campaign_recipients.count_documents(filt)
    skip = (page - 1) * page_size
    cur = (
        get_db()
        .campaign_recipients.find(filt)
        .sort(sort_field, -1)
        .skip(skip)
        .limit(page_size)
    )
    items = [serialize(d) async for d in cur]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@router.get("/campaigns/{cid}/analytics")
async def campaign_analytics(cid: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    doc = await _get_owned(cid, user_id)
    pipe = await get_db().campaign_recipients.aggregate(
        [{"$match": {"campaign_id": cid}}, {"$group": {"_id": "$status", "n": {"$sum": 1}}}]
    ).to_list(50)
    status_counts = {str(r["_id"]): int(r["n"]) for r in pipe}
    rates = compute_rates(doc, status_counts)

    fail_cur = (
        get_db()
        .campaign_recipients.find({"campaign_id": cid, "status": "failed"})
        .sort("updated_at", -1)
        .limit(10)
    )
    recent_failures = [serialize(d) async for d in fail_cur]

    err_pipe = await get_db().campaign_recipients.aggregate(
        [
            {
                "$match": {
                    "campaign_id": cid,
                    "$or": [
                        {"error_code": {"$nin": [None, ""]}},
                        {"error_message": {"$nin": [None, ""]}},
                    ],
                }
            },
            {
                "$project": {
                    "reason": {
                        "$ifNull": [
                            {"$cond": [{"$eq": ["$error_code", ""]}, None, "$error_code"]},
                            "$error_message",
                        ]
                    }
                }
            },
            {"$match": {"reason": {"$nin": [None, ""]}}},
            {"$group": {"_id": "$reason", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
            {"$limit": 8},
        ]
    ).to_list(8)
    top_errors = [{"message": r["_id"], "count": r["n"]} for r in err_pipe]

    return {
        "campaign": serialize(doc),
        "status_counts": status_counts,
        "rates": rates,
        "progress_percentage": progress_percentage(doc),
        "recent_failures": recent_failures,
        "top_errors": top_errors,
    }


# ─── Existing blast endpoints (unchanged behaviour) ───────────────────────────

@router.get("/blasts")
async def list_blasts(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().blast_campaigns.find({"user_id": str(user["_id"])}).sort("created_at", -1)
    return [serialize(d) async for d in cur]


@router.post("/blasts", status_code=202)
async def create_blast(payload: BlastCreate, user: dict = Depends(current_user)) -> dict:
    from app.config import settings
    from app.models.message import TEMPLATE_ALLOWED_PURPOSES
    from app.security.rate_limit import rate_limit_campaign
    from app.security.validation import limit_list, limit_template_variables
    from app.services.throughput import assert_bulk_enqueue_allowed
    from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility

    user_id = str(user["_id"])
    rate_limit_campaign(user_id)
    db = get_db()
    payload.recipients = limit_list(
        payload.recipients,
        max_items=int(settings.CAMPAIGN_MAX_RECIPIENTS_PER_REQUEST),
        field="recipients",
    )
    if payload.content_variables is not None:
        payload.content_variables = limit_template_variables(payload.content_variables)

    content_sid = None
    content_variables = payload.content_variables
    template_id = (payload.template_id or "").strip() or None
    message = (payload.message or "").strip() or None
    media_url = (payload.media_url or "").strip() or None
    purpose = (payload.message_purpose or "").strip().lower() or None

    if template_id:
        from app.routes.templates import get_approved_template

        if media_url:
            raise HTTPException(
                status_code=400,
                detail="Cannot attach media to template blasts",
            )
        tmpl = await get_approved_template(user_id, template_id)
        content_sid = tmpl["content_sid"]
        template_id = str(tmpl["_id"])
        if not message:
            message = f"Template: {tmpl.get('name') or content_sid}"
        # Templates need an explicit purpose (same rule as Live Chat).
        if not purpose or purpose not in TEMPLATE_ALLOWED_PURPOSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "message_purpose is required for template blasts and must be one of: "
                    + ", ".join(sorted(TEMPLATE_ALLOWED_PURPOSES))
                ),
            )
    else:
        if not message and not media_url:
            raise HTTPException(
                status_code=400,
                detail="Provide message text, media_url, or an approved template_id",
            )
        # Free-form defaults to conversational (not marketing) so sandbox tests work.
        purpose = purpose or "conversational"
        if purpose not in TEMPLATE_ALLOWED_PURPOSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "message_purpose must be one of: "
                    + ", ".join(sorted(TEMPLATE_ALLOWED_PURPOSES))
                ),
            )

    assert_bulk_enqueue_allowed(estimated_jobs=len(payload.recipients))

    blast_doc = {
        "user_id": user_id,
        "name": payload.name.strip(),
        "message": message or (f"[media]" if media_url else ""),
        "template_id": template_id,
        "content_sid": content_sid,
        "content_variables": content_variables,
        "media_url": media_url if not content_sid else None,
        "message_purpose": purpose,
        "total_recipients": len(payload.recipients),
        "sent_count": 0,
        "failed_count": 0,
        "status": "queued",
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    res = await db.blast_campaigns.insert_one(blast_doc)
    blast_id = str(res.inserted_id)

    blacklist = {
        d["phone"]
        async for d in db.blacklist.find({"user_id": user_id}, {"phone": 1})
    }

    rows = []
    seen: set[str] = set()
    skip_reasons: dict[str, int] = {}
    for raw in payload.recipients:
        try:
            normalized = to_whatsapp(raw).replace("whatsapp:", "")
        except Exception:
            skip_reasons["invalid_phone"] = skip_reasons.get("invalid_phone", 0) + 1
            continue
        if normalized in seen:
            skip_reasons["duplicate"] = skip_reasons.get("duplicate", 0) + 1
            continue
        seen.add(normalized)
        lead = await db.leads.find_one({"user_id": user_id, "phone": normalized})
        elig = get_whatsapp_send_eligibility(
            lead=lead or {"phone": normalized, "blacklisted": normalized in blacklist},
            phone=normalized,
            purpose=purpose,  # type: ignore[arg-type]
            has_template=bool(content_sid),
            blacklisted=normalized in blacklist,
        )
        if not elig.allowed:
            key = elig.reason_code or "blocked"
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
            continue
        rows.append({
            "blast_id": blast_id,
            "user_id": user_id,
            "phone": normalized,
            "status": "pending",
            "message_purpose": purpose,
            "attempt_count": 0,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        })
    if rows:
        await db.blast_recipients.insert_many(rows)
        await db.blast_campaigns.update_one(
            {"_id": res.inserted_id}, {"$set": {"total_recipients": len(rows)}}
        )
    else:
        reason_bits = ", ".join(f"{k}={v}" for k, v in sorted(skip_reasons.items())) or "unknown"
        hint = (
            "No eligible recipients"
            + (f" ({reason_bits})" if reason_bits else "")
            + ". Marketing requires WhatsApp opt-in; use purpose Conversational/Support with an approved template for testing."
        )
        await db.blast_campaigns.delete_one({"_id": res.inserted_id})
        raise HTTPException(status_code=400, detail=hint)

    enqueue(tasks.send_blast_messages, user_id, blast_id, queue="bulk")
    fresh = await db.blast_campaigns.find_one({"_id": res.inserted_id})
    return serialize(fresh)


_BLAST_TERMINAL_STATUSES = frozenset({"completed", "partially_completed", "failed", "cancelled"})


@router.post("/blasts/{bid}/pause")
async def pause_blast(bid: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    if not ObjectId.is_valid(bid):
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db().blast_campaigns.find_one({"_id": ObjectId(bid), "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    if doc.get("status") != "sending":
        raise HTTPException(status_code=400, detail="Only sending blasts can pause")
    await get_db().blast_campaigns.update_one(
        {"_id": ObjectId(bid)}, {"$set": {"status": "paused", "updated_at": utcnow()}}
    )
    fresh = await get_db().blast_campaigns.find_one({"_id": ObjectId(bid)})
    await ws_manager.push(user_id, "blast:updated", serialize(fresh))
    return serialize(fresh)


@router.post("/blasts/{bid}/resume")
async def resume_blast(bid: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    if not ObjectId.is_valid(bid):
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db().blast_campaigns.find_one({"_id": ObjectId(bid), "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    if doc.get("status") != "paused":
        raise HTTPException(status_code=400, detail="Only paused blasts can resume")
    await get_db().blast_campaigns.update_one(
        {"_id": ObjectId(bid)}, {"$set": {"status": "sending", "updated_at": utcnow()}}
    )
    enqueue(tasks.send_blast_messages, user_id, bid, queue="bulk")
    fresh = await get_db().blast_campaigns.find_one({"_id": ObjectId(bid)})
    await ws_manager.push(user_id, "blast:updated", serialize(fresh))
    return serialize(fresh)


@router.post("/blasts/{bid}/cancel")
async def cancel_blast(bid: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    if not ObjectId.is_valid(bid):
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db().blast_campaigns.find_one({"_id": ObjectId(bid), "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    if doc.get("status") in _BLAST_TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail="Blast already finished")
    now = utcnow()
    await get_db().blast_campaigns.update_one(
        {"_id": ObjectId(bid)}, {"$set": {"status": "cancelled", "updated_at": now}}
    )
    await get_db().blast_recipients.update_many(
        {"blast_id": bid, "status": {"$in": ["pending", "processing", "retrying"]}},
        {"$set": {"status": "cancelled", "updated_at": now}},
    )
    fresh = await get_db().blast_campaigns.find_one({"_id": ObjectId(bid)})
    await ws_manager.push(user_id, "blast:cancelled", serialize(fresh))
    return serialize(fresh)


@router.get("/blasts/{bid}/recipients")
async def list_blast_recipients(bid: str, user: dict = Depends(current_user)) -> list[dict]:
    db = get_db()
    if not ObjectId.is_valid(bid):
        raise HTTPException(status_code=404, detail="Not found")
    blast = await db.blast_campaigns.find_one({"_id": ObjectId(bid), "user_id": str(user["_id"])})
    if not blast:
        raise HTTPException(status_code=404, detail="Not found")
    cur = db.blast_recipients.find({"blast_id": bid}).sort("created_at", 1)
    return [serialize(d) async for d in cur]


@router.delete("/blasts/{bid}", status_code=204)
async def delete_blast(bid: str, user: dict = Depends(current_user)) -> Response:
    if not ObjectId.is_valid(bid):
        raise HTTPException(status_code=404, detail="Not found")
    db = get_db()
    res = await db.blast_campaigns.delete_one({"_id": ObjectId(bid), "user_id": str(user["_id"])})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    await db.blast_recipients.delete_many({"blast_id": bid})
    return Response(status_code=204)
