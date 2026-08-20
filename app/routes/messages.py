from fastapi import APIRouter, Depends, HTTPException, Query, Request, Header
from fastapi.responses import StreamingResponse
from bson import ObjectId
import httpx
from typing import Annotated

from app.config import settings
from app.db.mongo import get_db
from app.middleware.auth import current_user, decode_token
from app.models.message import MessageSend, TEMPLATE_ALLOWED_PURPOSES
from app.models.common import serialize
from app.services import message_service, lead_service
from app.services.inbound_whatsapp import resolve_lead_whatsapp_provider
from app.services.media import media_fields_from_items
from app.services.ws_manager import ws_manager
from app.workers.queue import enqueue
from app.workers import tasks
from app.routes.templates import get_approved_template, get_sendable_meta_template

router = APIRouter(tags=["messages"])


def _absolute_media_url(media_url: str | None) -> str | None:
    """Twilio must fetch media from a public HTTPS URL."""
    if not media_url:
        return None
    url = media_url.strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise HTTPException(
            status_code=400,
            detail=(
                "PUBLIC_BASE_URL is not set. Twilio cannot download your file from localhost. "
                "Set PUBLIC_BASE_URL to your Cloudflare tunnel HTTPS URL and restart the API."
            ),
        )
    if not url.startswith("/"):
        url = "/" + url
    return f"{base}{url}"


@router.get("/messages/{lead_id}")
async def list_messages(lead_id: str, user: dict = Depends(current_user)) -> list[dict]:
    lead = await lead_service.get_lead(str(user["_id"]), lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    docs = await message_service.list_messages(str(user["_id"]), lead_id)
    return [serialize(d) for d in docs]


async def _user_from_bearer_or_query(request: Request, access_token: str | None) -> dict:
    """Allow Bearer header or ?access_token= for <img>/<audio> tags."""
    auth = request.headers.get("Authorization") or ""
    token = None
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    elif access_token:
        token = access_token.strip()
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = decode_token(token)
        user_id = payload.get("sub")
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token") from None
    if not user_id or not ObjectId.is_valid(user_id):
        raise HTTPException(status_code=401, detail="Invalid token")
    user = await get_db().users.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


@router.get("/messages/{message_id}/media/{media_index}")
async def proxy_message_media(
    message_id: str,
    media_index: int,
    request: Request,
    access_token: str | None = Query(default=None),
):
    """Tenant-safe proxy for Twilio inbound media (never exposes auth token to clients)."""
    user = await _user_from_bearer_or_query(request, access_token)
    user_id = str(user["_id"])
    if not ObjectId.is_valid(message_id):
        raise HTTPException(status_code=404, detail="Not found")
    if media_index < 0 or media_index > 20:
        raise HTTPException(status_code=404, detail="Not found")

    msg = await get_db().messages.find_one({"_id": ObjectId(message_id), "user_id": user_id})
    if not msg:
        raise HTTPException(status_code=404, detail="Not found")

    items = msg.get("media_items") or []
    twilio_url = None
    content_type = None
    filename = None
    if 0 <= media_index < len(items):
        item = items[media_index]
        twilio_url = item.get("url")
        content_type = item.get("content_type")
        filename = item.get("filename")
    elif media_index == 0 and msg.get("media_url"):
        twilio_url = msg.get("media_url")
        content_type = msg.get("media_content_type")
        filename = msg.get("media_filename")

    if not twilio_url:
        raise HTTPException(status_code=404, detail="Media not found")

    # Local/outbound stored files — redirect-style stream via our public file route data
    if "/api/media/files/" in twilio_url or twilio_url.startswith("/api/media/files/"):
        from app.services.media_storage import get_media_storage

        file_id = twilio_url.rstrip("/").split("/")[-1]
        try:
            stream, ct, fn = get_media_storage().open(file_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="Media not found") from None
        headers = {}
        if fn or filename:
            headers["Content-Disposition"] = f'inline; filename="{fn or filename}"'
        return StreamingResponse(stream, media_type=ct or content_type or "application/octet-stream", headers=headers)

    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise HTTPException(status_code=503, detail="Twilio not configured")

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await client.get(
                twilio_url,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch media: {exc}") from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail="Upstream media fetch failed")

    ct = content_type or resp.headers.get("content-type") or "application/octet-stream"
    headers = {}
    if filename:
        headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return StreamingResponse(
        iter([resp.content]),
        media_type=ct.split(";")[0].strip(),
        headers=headers,
    )


@router.post("/send-message", status_code=202)
async def send_message(
    payload: MessageSend,
    user: dict = Depends(current_user),
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    from app.security.rate_limit import rate_limit_send
    from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility
    from app.services.idempotency import claim_idempotency, get_idempotency_result, store_idempotency_result
    from app.observability.metrics import inc_policy_blocked, inc_duplicate_prevented
    from app.security.audit import audit
    from app.middleware.security import get_request_id

    rate_limit_send(str(user["_id"]))
    user_id = str(user["_id"])
    lead = await lead_service.get_lead(user_id, payload.lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    if not lead.get("phone"):
        raise HTTPException(status_code=400, detail="Lead has no phone number")

    provider = await resolve_lead_whatsapp_provider(user_id, payload.lead_id, db=get_db())

    idem_key = (idempotency_key or "").strip()
    if idem_key:
        cached = get_idempotency_result(user_id, idem_key)
        if cached:
            inc_duplicate_prevented()
            return cached
        if not claim_idempotency(user_id, idem_key):
            cached = get_idempotency_result(user_id, idem_key)
            if cached:
                inc_duplicate_prevented()
                return cached
            raise HTTPException(status_code=409, detail="Duplicate request in progress")

    content_sid = (payload.content_sid or "").strip() or None
    content_variables = payload.content_variables
    template_id = (payload.template_id or "").strip() or None
    body = (payload.message or "").strip() or None
    media_url = (payload.media_url or "").strip() or None
    media_content_type = (payload.media_content_type or "").strip() or None
    media_filename = (payload.media_filename or "").strip() or None
    has_template_request = bool(template_id or content_sid)
    purpose = (payload.message_purpose or "").strip().lower() or None
    tmpl = None
    meta_graph_components = None
    meta_template_name = None
    meta_language_code = None

    if provider == "meta":
        if media_url or media_content_type or media_filename:
            raise HTTPException(
                status_code=400,
                detail="Media sending is not yet supported for Meta WhatsApp conversations.",
            )
        if content_sid:
            raise HTTPException(
                status_code=400,
                detail="Twilio Content templates cannot be sent on Meta WhatsApp conversations.",
            )

    # Live Chat marketing template consent: templates must never silently
    # default to conversational — the caller must pick an explicit, valid purpose.
    if has_template_request:
        if not purpose or purpose not in TEMPLATE_ALLOWED_PURPOSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    "message_purpose is required when sending a template and must be one of: "
                    + ", ".join(sorted(TEMPLATE_ALLOWED_PURPOSES))
                ),
            )
    else:
        purpose = purpose or "conversational"

    if provider == "meta" and template_id:
        from app.services.meta_templates import MetaTemplateError, build_graph_components

        tmpl = await get_sendable_meta_template(user_id, template_id)
        try:
            meta_graph_components = build_graph_components(
                template=tmpl, content_variables=content_variables
            )
        except MetaTemplateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        template_id = str(tmpl["_id"])
        meta_template_name = (tmpl.get("meta_template_name") or "").strip()
        meta_language_code = (tmpl.get("meta_language_code") or "").strip()
        content_sid = None
    elif template_id:
        if media_url:
            raise HTTPException(status_code=400, detail="Cannot attach media to template messages")
        tmpl = await get_approved_template(user_id, template_id)
        content_sid = tmpl["content_sid"]
        template_id = str(tmpl["_id"])
    elif content_sid:
        if media_url:
            raise HTTPException(status_code=400, detail="Cannot attach media to template messages")
        tmpl = await get_db().templates.find_one(
            {
                "user_id": user_id,
                "content_sid": content_sid,
                "status": "approved",
            }
        )
        if not tmpl:
            raise HTTPException(
                status_code=400,
                detail="content_sid must belong to an approved template for this account",
            )
        if (tmpl.get("provider") or "twilio_content") == "meta":
            raise HTTPException(
                status_code=400,
                detail="Meta templates cannot be sent as Twilio Content templates",
            )
        template_id = str(tmpl["_id"])

    has_template = bool(content_sid) or bool(meta_template_name)

    elig = get_whatsapp_send_eligibility(
        lead=lead,
        purpose=purpose,  # type: ignore[arg-type]
        has_template=has_template,
        has_media=bool(media_url),
        provider=provider,
        user=user,
    )
    if not elig.allowed:
        inc_policy_blocked(elig.reason_code)
        audit(
            "message.blocked_by_policy",
            user_id=user_id,
            target_id=payload.lead_id,
            request_id=get_request_id(),
        )
        raise HTTPException(status_code=400, detail=elig.safe_message)

    # Resolve to public absolute URL before queueing (Twilio cannot fetch localhost/relative).
    media_url = _absolute_media_url(media_url)

    media_items = []
    if media_url:
        media_items = [
            {
                "url": media_url,
                "content_type": media_content_type or "application/octet-stream",
                "filename": media_filename,
                "index": 0,
            }
        ]
    media_meta = media_fields_from_items(media_items, body or "")
    template_name = None
    if tmpl:
        template_name = (tmpl.get("name") or "").strip() or None
    if meta_template_name and not body:
        display_message = f"Template: {template_name or meta_template_name}"
        out_message_type = "template"
    elif content_sid and not body:
        display_message = f"Template: {template_name or content_sid}"
        out_message_type = "text"
    elif body:
        display_message = body
        out_message_type = media_meta["message_type"] if media_items else "text"
    else:
        display_message = media_filename or f"[{media_meta['message_type']}]"
        out_message_type = media_meta["message_type"] if media_items else "text"

    doc = await message_service.insert_message(
        user_id=user_id,
        lead_id=payload.lead_id,
        direction="outbound",
        message=display_message,
        status="queued",
        template_id=template_id,
        content_sid=content_sid,
        content_variables=content_variables,
        message_type=out_message_type,
        media_items=media_items or None,
        media_url=media_meta.get("media_url"),
        media_content_type=media_meta.get("media_content_type"),
        media_filename=media_meta.get("media_filename"),
        provider=provider,
        sender_type="human",
        provider_message_id=None,
    )
    extra_set: dict = {
        "message_purpose": purpose,
        "template_name": template_name,
        "consent_status_at_send": elig.consent_status,
        "window_open_at_send": elig.window_status == "open",
        "policy_decision": "allowed",
        "policy_reason": elig.reason_code,
        "idempotency_key": idem_key or None,
        "provider": provider,
        "sender_type": "human",
        "provider_message_id": None,
    }
    if meta_template_name:
        extra_set["meta_template_name"] = meta_template_name
        extra_set["meta_language_code"] = meta_language_code
        extra_set["message_type"] = "template"
        extra_set["twilio_sid"] = None
        extra_set["content_sid"] = None
    if provider != "meta":
        extra_set["sender_number"] = (settings.TWILIO_WHATSAPP_FROM or "")[:40] or None
    await get_db().messages.update_one({"_id": doc["_id"]}, {"$set": extra_set})
    doc = await get_db().messages.find_one({"_id": doc["_id"]}) or doc
    serialized = serialize(doc)
    await ws_manager.push(user_id, "message:new", serialized)

    enqueue(
        tasks.send_outbound_message,
        str(doc["_id"]),
        user_id,
        payload.lead_id,
        body,
        media_url=media_url,
        content_sid=content_sid,
        content_variables=content_variables,
        queue="high",
    )
    if idem_key:
        store_idempotency_result(user_id, idem_key, serialized)
    return serialized


@router.post("/messages/check-eligibility")
async def check_eligibility(payload: dict, user: dict = Depends(current_user)) -> dict:
    """Preview-only eligibility check. Worker re-checks before send."""
    from app.services.whatsapp_eligibility import get_whatsapp_send_eligibility

    user_id = str(user["_id"])
    lead = None
    lead_id = (payload.get("lead_id") or "").strip()
    phone = (payload.get("phone") or "").strip() or None
    if lead_id:
        lead = await lead_service.get_lead(user_id, lead_id)
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
    elif phone:
        lead = await get_db().leads.find_one({"user_id": user_id, "phone": phone})
    purpose = payload.get("message_purpose") or "conversational"
    template_id = (payload.get("template_id") or "").strip()
    has_template = bool(template_id or payload.get("content_sid"))
    has_media = bool(payload.get("has_media"))
    preview_provider = "twilio"
    if lead_id:
        preview_provider = await resolve_lead_whatsapp_provider(user_id, lead_id, db=get_db())
    result = get_whatsapp_send_eligibility(
        lead=lead,
        phone=phone or (lead or {}).get("phone"),
        purpose=purpose,
        has_template=has_template,
        has_media=has_media,
        provider=preview_provider or "twilio",
        user=user,
    )
    return result.to_dict()
