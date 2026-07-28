from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Any, Optional, Literal, Annotated
import csv
import io
from datetime import datetime, timezone

from app.config import settings
from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.middleware.security import get_request_id
from app.models.lead import LeadCreate, LeadUpdate
from app.models.common import serialize, utcnow
from app.security.audit import audit
from app.security.validation import patch_allowlist, reject_mongo_operators, require_object_id
from app.services import lead_service
from app.services.consent_ops import apply_consent_change, consent_snapshot
from app.services.lead_query import csv_safe_cell, iter_leads_for_export, query_leads_page
from app.services.lead_import import import_leads_csv
from app.services.lead_bulk import run_bulk_action
from app.services.ws_manager import ws_manager

router = APIRouter(prefix="/leads", tags=["leads"])

_LEAD_PATCH_FIELDS = {
    "name",
    "phone",
    "score",
    "source",
    "tags",
    "blacklisted",
}


class ConsentBody(BaseModel):
    source: Optional[str] = Field(default="manual", max_length=40)
    proof: Optional[str] = Field(default=None, max_length=500)
    reason: Optional[str] = Field(default=None, max_length=200)


class BulkActionBody(BaseModel):
    lead_ids: list[str] = Field(min_length=1)
    action: str = Field(min_length=1, max_length=40)
    value: Any = None


async def _publish_lead(user_id: str, doc: dict) -> dict:
    payload = serialize(doc)
    await ws_manager.push(user_id, "lead:updated", payload)
    return payload


def _list_filter_kwargs(
    search: Optional[str] = None,
    score: Optional[str] = None,
    consent_status: Optional[str] = None,
    blacklist_status: Optional[str] = None,
    agent_id: Optional[str] = None,
    source: Optional[str] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    updated_from: Optional[str] = None,
    updated_to: Optional[str] = None,
    last_inbound_from: Optional[str] = None,
    last_inbound_to: Optional[str] = None,
    ai_paused: Optional[str] = None,
    needs_human: Optional[str] = None,
    takeover_active: Optional[str] = None,
    window_status: Optional[str] = None,
    lead_score_min: Optional[int] = None,
    lead_score_max: Optional[int] = None,
) -> dict:
    return {
        "search": search,
        "score": score,
        "consent_status": consent_status,
        "blacklist_status": blacklist_status,
        "agent_id": agent_id,
        "source": source,
        "created_from": created_from,
        "created_to": created_to,
        "updated_from": updated_from,
        "updated_to": updated_to,
        "last_inbound_from": last_inbound_from,
        "last_inbound_to": last_inbound_to,
        "ai_paused": ai_paused,
        "needs_human": needs_human,
        "takeover_active": takeover_active,
        "window_status": window_status,
        "lead_score_min": lead_score_min,
        "lead_score_max": lead_score_max,
    }


@router.get("")
async def list_leads(
    user: dict = Depends(current_user),
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int | None, Query()] = None,
    search: Annotated[Optional[str], Query(max_length=100)] = None,
    score: Annotated[Optional[str], Query()] = None,
    consent_status: Annotated[Optional[str], Query()] = None,
    blacklist_status: Annotated[Optional[str], Query()] = None,
    agent_id: Annotated[Optional[str], Query()] = None,
    source: Annotated[Optional[str], Query(max_length=100)] = None,
    created_from: Annotated[Optional[str], Query()] = None,
    created_to: Annotated[Optional[str], Query()] = None,
    updated_from: Annotated[Optional[str], Query()] = None,
    updated_to: Annotated[Optional[str], Query()] = None,
    last_inbound_from: Annotated[Optional[str], Query()] = None,
    last_inbound_to: Annotated[Optional[str], Query()] = None,
    ai_paused: Annotated[Optional[str], Query()] = None,
    needs_human: Annotated[Optional[str], Query()] = None,
    takeover_active: Annotated[Optional[str], Query()] = None,
    window_status: Annotated[Optional[str], Query()] = None,
    lead_score_min: Annotated[Optional[int], Query()] = None,
    lead_score_max: Annotated[Optional[int], Query()] = None,
    sort_by: Annotated[Optional[str], Query()] = "updated_at",
    sort_order: Annotated[Optional[str], Query()] = "desc",
) -> dict:
    user_id = str(user["_id"])
    return await query_leads_page(
        get_db(),
        user_id,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
        **_list_filter_kwargs(
            search=search,
            score=score,
            consent_status=consent_status,
            blacklist_status=blacklist_status,
            agent_id=agent_id,
            source=source,
            created_from=created_from,
            created_to=created_to,
            updated_from=updated_from,
            updated_to=updated_to,
            last_inbound_from=last_inbound_from,
            last_inbound_to=last_inbound_to,
            ai_paused=ai_paused,
            needs_human=needs_human,
            takeover_active=takeover_active,
            window_status=window_status,
            lead_score_min=lead_score_min,
            lead_score_max=lead_score_max,
        ),
    )


@router.get("/options")
async def lead_options(
    user: dict = Depends(current_user),
    search: Optional[str] = Query(default=None, max_length=100),
    limit: int = Query(default=25, ge=1, le=50),
) -> list[dict]:
    """Lightweight lead picker for Live Chat / Campaigns — never returns full dataset."""
    from app.config import settings as cfg

    max_lim = max(1, int(cfg.LEAD_OPTIONS_MAX_LIMIT))
    lim = min(limit, max_lim)
    result = await query_leads_page(
        get_db(),
        str(user["_id"]),
        page=1,
        page_size=lim,
        search=search,
        sort_by="updated_at",
        sort_order="desc",
    )
    out = []
    for item in result["items"]:
        out.append(
            {
                "id": item["id"],
                "name": item.get("name"),
                "phone": item.get("phone"),
                "score": item.get("score"),
                "blacklisted": bool(item.get("blacklisted")),
                "whatsapp_consent_status": item.get("whatsapp_consent_status") or "unknown",
            }
        )
    return out


@router.post("/import")
async def import_leads(
    user: dict = Depends(current_user),
    file: UploadFile = File(...),
    duplicate_policy: Literal["skip", "update", "fail"] = Form(default="skip"),
) -> dict:
    from app.security.rate_limit import rate_limit_upload

    user_id = str(user["_id"])
    rate_limit_upload(user_id)
    summary = await import_leads_csv(
        get_db(),
        user_id=user_id,
        upload=file,
        duplicate_policy=duplicate_policy,
    )
    audit("leads.import", user_id=user_id, request_id=get_request_id())
    from app.services.activity import record_activity
    from app.services.notifications import create_notification

    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="leads.import",
        summary="Lead import finished",
        actor_id=user_id,
        resource_type="leads",
        resource_id=user_id,
        metadata={
            "created": summary.get("created"),
            "updated": summary.get("updated"),
            "skipped": summary.get("skipped"),
        },
    )
    await create_notification(
        get_db(),
        user_id=user_id,
        type="import_completed",
        title="Import completed",
        message="Your lead import has finished.",
        resource_type="leads",
        resource_id=user_id,
        dedupe_key=f"import:{user_id}:{int(utcnow().timestamp())}",
    )
    return summary

@router.get("/export")
async def export_leads(
    user: dict = Depends(current_user),
    search: Optional[str] = Query(default=None, max_length=100),
    score: Optional[str] = Query(default=None),
    consent_status: Optional[str] = Query(default=None),
    blacklist_status: Optional[str] = Query(default=None),
    agent_id: Optional[str] = Query(default=None),
    source: Optional[str] = Query(default=None, max_length=100),
    created_from: Optional[str] = Query(default=None),
    created_to: Optional[str] = Query(default=None),
    updated_from: Optional[str] = Query(default=None),
    updated_to: Optional[str] = Query(default=None),
    last_inbound_from: Optional[str] = Query(default=None),
    last_inbound_to: Optional[str] = Query(default=None),
    ai_paused: Optional[str] = Query(default=None),
    needs_human: Optional[str] = Query(default=None),
    takeover_active: Optional[str] = Query(default=None),
    window_status: Optional[str] = Query(default=None),
    lead_score_min: Optional[int] = Query(default=None),
    lead_score_max: Optional[int] = Query(default=None),
) -> StreamingResponse:
    user_id = str(user["_id"])
    max_rows = max(1, int(settings.LEAD_EXPORT_MAX_ROWS))
    filters = _list_filter_kwargs(
        search=search,
        score=score,
        consent_status=consent_status,
        blacklist_status=blacklist_status,
        agent_id=agent_id,
        source=source,
        created_from=created_from,
        created_to=created_to,
        updated_from=updated_from,
        updated_to=updated_to,
        last_inbound_from=last_inbound_from,
        last_inbound_to=last_inbound_to,
        ai_paused=ai_paused,
        needs_human=needs_human,
        takeover_active=takeover_active,
        window_status=window_status,
        lead_score_min=lead_score_min,
        lead_score_max=lead_score_max,
    )
    audit("leads.export", user_id=user_id, request_id=get_request_id())

    headers = [
        "name",
        "phone",
        "email",
        "company",
        "source",
        "score",
        "lead_score",
        "consent_status",
        "consent_source",
        "consent_at",
        "opted_out_at",
        "blacklisted",
        "ai_paused",
        "needs_human",
        "created_at",
        "updated_at",
        "last_inbound_at",
    ]

    def _fmt(v: Any) -> str:
        if isinstance(v, datetime):
            return v.astimezone(timezone.utc).isoformat()
        return csv_safe_cell(v)

    async def row_iter():
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(headers)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        async for doc in iter_leads_for_export(
            get_db(), user_id, max_rows=max_rows, **filters
        ):
            writer.writerow(
                [
                    _fmt(doc.get("name")),
                    _fmt(doc.get("phone")),
                    _fmt(doc.get("email")),
                    _fmt(doc.get("company")),
                    _fmt(doc.get("source")),
                    _fmt(doc.get("score")),
                    _fmt(doc.get("lead_score")),
                    _fmt(doc.get("whatsapp_consent_status") or "unknown"),
                    _fmt(doc.get("whatsapp_consent_source")),
                    _fmt(doc.get("whatsapp_consent_at")),
                    _fmt(doc.get("whatsapp_opted_out_at")),
                    _fmt(bool(doc.get("blacklisted"))),
                    _fmt(bool(doc.get("ai_paused"))),
                    _fmt(bool(doc.get("needs_human"))),
                    _fmt(doc.get("created_at")),
                    _fmt(doc.get("updated_at")),
                    _fmt(doc.get("last_inbound_at")),
                ]
            )
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    filename = f"leads-export-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv"
    return StreamingResponse(
        row_iter(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/sample.csv")
async def sample_csv(user: dict = Depends(current_user)) -> StreamingResponse:
    _ = user
    content = (
        "name,phone,email,company,source,tags,consent_status,consent_source,consent_at\n"
        "Jane Doe,+447700900123,jane@example.com,Acme,website,\"demo,pricing\",unknown,,\n"
    )
    return StreamingResponse(
        iter([content]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leads-sample.csv"'},
    )


@router.post("/bulk-action")
async def bulk_action(body: BulkActionBody, user: dict = Depends(current_user)) -> dict:
    reject_mongo_operators(body.model_dump())
    return await run_bulk_action(
        get_db(),
        user_id=str(user["_id"]),
        lead_ids=body.lead_ids,
        action=body.action,
        value=body.value,
        request_id=get_request_id(),
    )


@router.post("", status_code=201)
async def create_lead(payload: LeadCreate, user: dict = Depends(current_user)) -> dict:
    doc = await lead_service.create_lead(str(user["_id"]), payload.model_dump())
    return serialize(doc)


@router.get("/{lead_id}")
async def get_lead(lead_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(lead_id)
    doc = await lead_service.get_lead(str(user["_id"]), lead_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return serialize(doc)


@router.patch("/{lead_id}")
async def update_lead(lead_id: str, payload: LeadUpdate, user: dict = Depends(current_user)) -> dict:
    require_object_id(lead_id)
    raw = payload.model_dump(exclude_unset=True)
    reject_mongo_operators(raw)
    data = patch_allowlist(raw, _LEAD_PATCH_FIELDS)
    doc = await lead_service.update_lead(str(user["_id"]), lead_id, data)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return serialize(doc)


@router.delete("/{lead_id}", status_code=204)
async def delete_lead(lead_id: str, user: dict = Depends(current_user)) -> None:
    require_object_id(lead_id)
    ok = await lead_service.delete_lead(str(user["_id"]), lead_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")


@router.post("/{lead_id}/ai/pause")
async def pause_ai(lead_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(lead_id)
    user_id = str(user["_id"])
    doc = await lead_service.set_lead_control(user_id, lead_id, {"ai_paused": True})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    audit("lead.ai_pause", user_id=user_id, target_id=lead_id, request_id=get_request_id())
    return await _publish_lead(user_id, doc)


@router.post("/{lead_id}/ai/resume")
async def resume_ai(lead_id: str, user: dict = Depends(current_user)) -> dict:
    """Resume AI replies. Does not clear an active human takeover — use handback for that."""
    require_object_id(lead_id)
    user_id = str(user["_id"])
    doc = await lead_service.set_lead_control(user_id, lead_id, {"ai_paused": False})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    audit("lead.ai_resume", user_id=user_id, target_id=lead_id, request_id=get_request_id())
    return await _publish_lead(user_id, doc)


@router.post("/{lead_id}/takeover")
async def take_over(lead_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(lead_id)
    user_id = str(user["_id"])
    doc = await lead_service.set_lead_control(
        user_id,
        lead_id,
        {
            "ai_paused": True,
            "needs_human": True,
            "takeover_by": user_id,
            "takeover_at": utcnow(),
        },
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    audit("lead.takeover", user_id=user_id, target_id=lead_id, request_id=get_request_id())
    from app.services.activity import record_activity
    from app.services.notifications import create_notification

    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="lead.takeover",
        summary="Human takeover started",
        actor_id=user_id,
        resource_type="lead",
        resource_id=lead_id,
    )
    await create_notification(
        get_db(),
        user_id=user_id,
        type="takeover_requested",
        title="Human takeover",
        message="A conversation was taken over by a human agent.",
        resource_type="lead",
        resource_id=lead_id,
        dedupe_key=f"takeover:{lead_id}:{user_id}",
    )
    return await _publish_lead(user_id, doc)


@router.post("/{lead_id}/handback")
async def hand_back(lead_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(lead_id)
    user_id = str(user["_id"])
    doc = await lead_service.set_lead_control(
        user_id,
        lead_id,
        {
            "ai_paused": False,
            "needs_human": False,
            "takeover_by": None,
            "takeover_at": None,
        },
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    audit("lead.handback", user_id=user_id, target_id=lead_id, request_id=get_request_id())
    return await _publish_lead(user_id, doc)


@router.post("/{lead_id}/needs-human")
async def mark_needs_human(lead_id: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    doc = await lead_service.set_lead_control(user_id, lead_id, {"needs_human": True})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.activity import record_activity
    from app.services.notifications import create_notification

    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="lead.needs_human",
        summary="Lead marked as needs human",
        actor_id=user_id,
        resource_type="lead",
        resource_id=lead_id,
    )
    await create_notification(
        get_db(),
        user_id=user_id,
        type="needs_human",
        title="Needs human attention",
        message="A lead was flagged as needing a human response.",
        resource_type="lead",
        resource_id=lead_id,
        dedupe_key=f"needs_human:{lead_id}",
    )
    return await _publish_lead(user_id, doc)


@router.delete("/{lead_id}/needs-human")
async def clear_needs_human(lead_id: str, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    doc = await lead_service.set_lead_control(user_id, lead_id, {"needs_human": False})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return await _publish_lead(user_id, doc)


@router.get("/{lead_id}/consent")
async def get_consent(lead_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(lead_id)
    user_id = str(user["_id"])
    doc = await lead_service.get_lead(user_id, lead_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    snap = consent_snapshot(doc)
    events = [
        serialize(e)
        async for e in get_db()
        .consent_events.find({"user_id": user_id, "lead_id": lead_id})
        .sort("created_at", -1)
        .limit(20)
    ]
    return {**snap, "recent_events": events}


@router.post("/{lead_id}/consent/opt-in")
async def consent_opt_in(
    lead_id: str, body: ConsentBody | None = None, user: dict = Depends(current_user)
) -> dict:
    require_object_id(lead_id)
    user_id = str(user["_id"])
    body = body or ConsentBody()
    lead = await lead_service.get_lead(user_id, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    updated = await apply_consent_change(
        get_db(),
        user_id=user_id,
        lead_id=lead_id,
        status="opted_in",
        source=body.source or "manual",
        proof=body.proof,
        changed_by=user_id,
        phone=lead.get("phone"),
        request_id=get_request_id(),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.lead_scoring import recalculate_lead_score
    from app.observability.metrics import inc_consent_opt_in

    await recalculate_lead_score(user_id, lead_id)
    inc_consent_opt_in()
    doc = await lead_service.get_lead(user_id, lead_id)
    return await _publish_lead(user_id, doc or updated)


@router.post("/{lead_id}/consent/opt-out")
async def consent_opt_out(
    lead_id: str, body: ConsentBody | None = None, user: dict = Depends(current_user)
) -> dict:
    require_object_id(lead_id)
    user_id = str(user["_id"])
    body = body or ConsentBody()
    lead = await lead_service.get_lead(user_id, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    updated = await apply_consent_change(
        get_db(),
        user_id=user_id,
        lead_id=lead_id,
        status="opted_out",
        source=body.source or "manual",
        proof=body.proof,
        reason=body.reason or "manual_opt_out",
        changed_by=user_id,
        phone=lead.get("phone"),
        request_id=get_request_id(),
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.lead_scoring import recalculate_lead_score
    from app.observability.metrics import inc_consent_opt_out

    await recalculate_lead_score(user_id, lead_id)
    inc_consent_opt_out()
    from app.services.activity import record_activity
    from app.services.notifications import create_notification

    await record_activity(
        get_db(),
        tenant_id=user_id,
        event_type="consent.opt_out",
        summary="Lead opted out of WhatsApp",
        actor_id=user_id,
        resource_type="lead",
        resource_id=lead_id,
    )
    await create_notification(
        get_db(),
        user_id=user_id,
        type="consent_opt_out",
        title="Consent opt-out",
        message="A lead opted out of WhatsApp messaging.",
        resource_type="lead",
        resource_id=lead_id,
        dedupe_key=f"optout:{lead_id}:{int(utcnow().timestamp()) // 60}",
    )
    doc = await lead_service.get_lead(user_id, lead_id)
    return await _publish_lead(user_id, doc or updated)


@router.get("/{lead_id}/whatsapp-history")
async def whatsapp_history(
    lead_id: str,
    user: dict = Depends(current_user),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict:
    """Tenant-safe compliance export: consent events + relevant messages."""
    require_object_id(lead_id)
    user_id = str(user["_id"])
    lead = await lead_service.get_lead(user_id, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    db = get_db()
    consent_events = [
        serialize(e)
        async for e in db.consent_events.find({"user_id": user_id, "lead_id": lead_id})
        .sort("created_at", -1)
        .skip(offset)
        .limit(limit)
    ]
    messages = [
        serialize(m)
        async for m in db.messages.find({"user_id": user_id, "lead_id": lead_id})
        .sort("created_at", -1)
        .skip(offset)
        .limit(limit)
    ]
    # Strip secrets if any accidentally present
    for m in messages:
        m.pop("auth_token", None)
        m.pop("twilio_auth_token", None)
    return {
        "lead_id": lead_id,
        "consent": consent_snapshot(lead),
        "consent_events": consent_events,
        "messages": messages,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{lead_id}/ai-suggestions")
async def list_ai_suggestions(lead_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(lead_id)
    user_id = str(user["_id"])
    lead = await lead_service.get_lead(user_id, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    items = [
        serialize(d)
        async for d in get_db()
        .ai_suggestions.find({"tenant_id": user_id, "lead_id": lead_id})
        .sort("created_at", -1)
        .limit(50)
    ]
    return {
        "items": items,
        "current_intent": lead.get("current_intent"),
        "current_sentiment": lead.get("current_sentiment"),
        "last_ai_error_category": lead.get("last_ai_error_category"),
    }


@router.post("/{lead_id}/ai-suggestions/{suggestion_id}/accept")
async def accept_ai_suggestion(
    lead_id: str, suggestion_id: str, user: dict = Depends(current_user)
) -> dict:
    require_object_id(lead_id)
    require_object_id(suggestion_id)
    user_id = str(user["_id"])
    lead = await lead_service.get_lead(user_id, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.ai_extraction import accept_suggestion
    from pymongo import MongoClient

    sync_db = MongoClient(settings.MONGO_URI)[settings.MONGO_DB]
    sug = accept_suggestion(
        sync_db, tenant_id=user_id, lead_id=lead_id, suggestion_id=suggestion_id, actor_id=user_id
    )
    if not sug:
        raise HTTPException(status_code=400, detail="Suggestion not found or not acceptable")
    audit(
        "lead.ai_suggestion_accept",
        user_id=user_id,
        target_id=lead_id,
        request_id=get_request_id(),
        extra={"field": sug.get("field")},
    )
    doc = await lead_service.get_lead(user_id, lead_id)
    return await _publish_lead(user_id, doc or lead)


@router.post("/{lead_id}/ai-suggestions/{suggestion_id}/reject")
async def reject_ai_suggestion(
    lead_id: str, suggestion_id: str, user: dict = Depends(current_user)
) -> dict:
    require_object_id(lead_id)
    require_object_id(suggestion_id)
    user_id = str(user["_id"])
    lead = await lead_service.get_lead(user_id, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    from app.services.ai_extraction import reject_suggestion
    from pymongo import MongoClient

    sync_db = MongoClient(settings.MONGO_URI)[settings.MONGO_DB]
    ok = reject_suggestion(
        sync_db, tenant_id=user_id, lead_id=lead_id, suggestion_id=suggestion_id, actor_id=user_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    audit(
        "lead.ai_suggestion_reject",
        user_id=user_id,
        target_id=lead_id,
        request_id=get_request_id(),
    )
    return {"ok": True}
