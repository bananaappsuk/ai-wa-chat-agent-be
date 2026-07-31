from fastapi import APIRouter, Depends, HTTPException, Query
from bson import ObjectId

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.middleware.security import get_request_id
from app.models.template import TemplateCreate, TemplateUpdate
from app.models.common import serialize, utcnow
from app.security.audit import audit
from app.security.validation import (
    limit_list,
    patch_allowlist,
    reject_mongo_operators,
    require_object_id,
    validate_content_sid,
)
from app.services.whatsapp_template_approval import (
    display_status_label,
    enrich_template_doc_from_info,
    mask_content_sid,
    normalize_whatsapp_approval_status,
    status_emoji,
)

router = APIRouter(prefix="/templates", tags=["templates"])

_TEMPLATE_PATCH = {"name", "content_sid", "language", "status", "variables"}


def _serialize_template(doc: dict, *, info: dict | None = None) -> dict:
    base = serialize(doc)
    if info:
        enriched = enrich_template_doc_from_info(base, info)
        for k, v in enriched.items():
            base[k] = v
        return base
    wa_raw = doc.get("whatsapp_approval_status")
    wa = normalize_whatsapp_approval_status(wa_raw) if wa_raw else None
    base["whatsapp_approval_status"] = wa_raw or None
    base["whatsapp_approval_label"] = display_status_label(wa) if wa else None
    base["whatsapp_approval_emoji"] = status_emoji(wa) if wa else None
    base["whatsapp_category"] = doc.get("whatsapp_category")
    base["content_sid_masked"] = mask_content_sid(doc.get("content_sid"))
    base["whatsapp_sendable"] = wa == "approved"
    base["provider"] = doc.get("provider") or "twilio_content"
    base["business_initiated"] = doc.get("business_initiated")
    base["user_initiated"] = doc.get("user_initiated")
    return base


@router.get("")
async def list_templates(
    user: dict = Depends(current_user),
    status: str | None = Query(default=None, description="Local library status filter"),
    whatsapp_status: str | None = Query(
        default=None,
        description="Filter by Meta approval: approved|pending|under_review|rejected|paused",
    ),
    q: str | None = Query(default=None, description="Search name / category / language"),
    refresh: bool = Query(default=False, description="Refresh Meta approval from Twilio"),
) -> list[dict]:
    cur = get_db().templates.find({"user_id": str(user["_id"])}).sort("updated_at", -1)
    rows: list[dict] = []
    q_l = (q or "").strip().lower()
    wa_filter = normalize_whatsapp_approval_status(whatsapp_status) if whatsapp_status else None
    async for d in cur:
        if status and (d.get("status") or "") != status:
            continue
        info = None
        if refresh and d.get("content_sid"):
            try:
                from app.services import twilio_service

                info = twilio_service.get_content_template_info(d["content_sid"])
                await get_db().templates.update_one(
                    {"_id": d["_id"]},
                    {
                        "$set": {
                            "whatsapp_approval_status": info.get("whatsapp_status"),
                            "whatsapp_category": info.get("whatsapp_category"),
                            "whatsapp_approval_checked_at": utcnow(),
                            "business_initiated": info.get("business_initiated"),
                            "user_initiated": info.get("user_initiated"),
                            "updated_at": utcnow(),
                        }
                    },
                )
                d["whatsapp_approval_status"] = info.get("whatsapp_status")
                d["whatsapp_category"] = info.get("whatsapp_category")
                d["business_initiated"] = info.get("business_initiated")
                d["user_initiated"] = info.get("user_initiated")
            except Exception:
                info = None
        ser = _serialize_template(d, info=info)
        if wa_filter and normalize_whatsapp_approval_status(ser.get("whatsapp_approval_status")) != wa_filter:
            continue
        if q_l:
            hay = " ".join(
                str(x or "")
                for x in (
                    ser.get("name"),
                    ser.get("friendly_name"),
                    ser.get("whatsapp_category"),
                    ser.get("language"),
                    ser.get("content_sid"),
                )
            ).lower()
            if q_l not in hay:
                continue
        rows.append(ser)
    return rows


@router.get("/{template_id}")
async def get_template(template_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(template_id)
    doc = await get_db().templates.find_one(
        {"_id": ObjectId(template_id), "user_id": str(user["_id"])}
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return _serialize_template(doc)


@router.post("/{template_id}/refresh-status")
async def refresh_template_status(template_id: str, user: dict = Depends(current_user)) -> dict:
    """Pull live Meta / Twilio WhatsApp approval status for a library template."""
    require_object_id(template_id)
    user_id = str(user["_id"])
    doc = await get_db().templates.find_one({"_id": ObjectId(template_id), "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    sid = (doc.get("content_sid") or "").strip()
    if not sid:
        raise HTTPException(status_code=400, detail="Template has no content_sid")
    from app.services import twilio_service

    try:
        info = twilio_service.get_content_template_info(sid)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Unable to refresh template status: {exc}") from exc

    await get_db().templates.update_one(
        {"_id": ObjectId(template_id), "user_id": user_id},
        {
            "$set": {
                "whatsapp_approval_status": info.get("whatsapp_status"),
                "whatsapp_category": info.get("whatsapp_category"),
                "whatsapp_approval_checked_at": utcnow(),
                "business_initiated": info.get("business_initiated"),
                "user_initiated": info.get("user_initiated"),
                "updated_at": utcnow(),
            }
        },
    )
    doc = await get_db().templates.find_one({"_id": ObjectId(template_id), "user_id": user_id})
    audit(
        "template.refresh_whatsapp_status",
        user_id=user_id,
        target_id=template_id,
        request_id=get_request_id(),
        extra={"whatsapp_status": info.get("whatsapp_status")},
    )
    return _serialize_template(doc or {}, info=info)


@router.post("", status_code=201)
async def create_template(payload: TemplateCreate, user: dict = Depends(current_user)) -> dict:
    doc = payload.model_dump()
    doc["content_sid"] = validate_content_sid(doc["content_sid"])
    doc["variables"] = limit_list(doc.get("variables") or [], max_items=50, field="variables")
    doc["user_id"] = str(user["_id"])
    doc["provider"] = "twilio_content"
    doc["created_at"] = utcnow()
    doc["updated_at"] = utcnow()
    try:
        from app.services import twilio_service

        info = twilio_service.get_content_template_info(doc["content_sid"])
        doc["whatsapp_approval_status"] = info.get("whatsapp_status")
        doc["whatsapp_category"] = info.get("whatsapp_category")
        doc["whatsapp_approval_checked_at"] = utcnow()
        doc["business_initiated"] = info.get("business_initiated")
        doc["user_initiated"] = info.get("user_initiated")
    except Exception:
        pass
    res = await get_db().templates.insert_one(doc)
    doc["_id"] = res.inserted_id
    audit(
        "template.create",
        user_id=str(user["_id"]),
        target_id=str(res.inserted_id),
        request_id=get_request_id(),
    )
    return _serialize_template(doc)


@router.patch("/{template_id}")
async def update_template(
    template_id: str, payload: TemplateUpdate, user: dict = Depends(current_user)
) -> dict:
    require_object_id(template_id)
    raw = payload.model_dump(exclude_unset=True)
    reject_mongo_operators(raw)
    update = patch_allowlist(raw, _TEMPLATE_PATCH)
    if "content_sid" in update and update["content_sid"] is not None:
        update["content_sid"] = validate_content_sid(update["content_sid"])
    if "variables" in update and update["variables"] is not None:
        update["variables"] = limit_list(update["variables"], max_items=50, field="variables")
    if not update:
        doc = await get_db().templates.find_one(
            {"_id": ObjectId(template_id), "user_id": str(user["_id"])}
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Not found")
        return _serialize_template(doc)
    update["updated_at"] = utcnow()
    res = await get_db().templates.update_one(
        {"_id": ObjectId(template_id), "user_id": str(user["_id"])}, {"$set": update}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db().templates.find_one(
        {"_id": ObjectId(template_id), "user_id": str(user["_id"])}
    )
    audit(
        "template.update",
        user_id=str(user["_id"]),
        target_id=template_id,
        request_id=get_request_id(),
    )
    return _serialize_template(doc or {})


@router.delete("/{template_id}", status_code=204)
async def delete_template(template_id: str, user: dict = Depends(current_user)) -> None:
    require_object_id(template_id)
    res = await get_db().templates.delete_one(
        {"_id": ObjectId(template_id), "user_id": str(user["_id"])}
    )
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    audit(
        "template.delete",
        user_id=str(user["_id"]),
        target_id=template_id,
        request_id=get_request_id(),
    )


async def get_approved_template(user_id: str, template_id: str) -> dict:
    if not ObjectId.is_valid(template_id):
        raise HTTPException(status_code=404, detail="Template not found")
    doc = await get_db().templates.find_one({"_id": ObjectId(template_id), "user_id": user_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Template not found")
    if doc.get("status") != "approved":
        raise HTTPException(status_code=400, detail="Template is not approved")
    if not doc.get("content_sid"):
        raise HTTPException(status_code=400, detail="Template has no content_sid")
    return doc
