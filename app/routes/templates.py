from fastapi import APIRouter, Depends, HTTPException
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

router = APIRouter(prefix="/templates", tags=["templates"])

_TEMPLATE_PATCH = {"name", "content_sid", "language", "status", "variables"}


@router.get("")
async def list_templates(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().templates.find({"user_id": str(user["_id"])}).sort("updated_at", -1)
    return [serialize(d) async for d in cur]


@router.post("", status_code=201)
async def create_template(payload: TemplateCreate, user: dict = Depends(current_user)) -> dict:
    doc = payload.model_dump()
    doc["content_sid"] = validate_content_sid(doc["content_sid"])
    doc["variables"] = limit_list(doc.get("variables") or [], max_items=50, field="variables")
    doc["user_id"] = str(user["_id"])
    doc["created_at"] = utcnow()
    doc["updated_at"] = utcnow()
    res = await get_db().templates.insert_one(doc)
    doc["_id"] = res.inserted_id
    audit(
        "template.create",
        user_id=str(user["_id"]),
        target_id=str(res.inserted_id),
        request_id=get_request_id(),
    )
    return serialize(doc)


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
        return serialize(doc)
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
    return serialize(doc)


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
