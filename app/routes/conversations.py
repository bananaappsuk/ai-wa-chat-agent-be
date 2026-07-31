"""Conversation summary endpoints."""
from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.common import serialize
from app.security.validation import require_object_id
from app.services.ai_summary import get_summary
from app.workers.queue import enqueue
from app.workers import ai_tasks

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.get("/{conversation_id}/summary")
async def get_conversation_summary(
    conversation_id: str, user: dict = Depends(current_user)
) -> dict:
    require_object_id(conversation_id)
    tenant_id = str(user["_id"])
    lead = await get_db().leads.find_one(
        {"_id": ObjectId(conversation_id), "user_id": tenant_id}
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db().conversation_summaries.find_one(
        {"tenant_id": tenant_id, "conversation_id": conversation_id}
    )
    if not doc:
        return {"summary": None, "updated_at": None, "summary_version": 0}
    return serialize(doc)


@router.post("/{conversation_id}/summary/refresh")
async def refresh_conversation_summary(
    conversation_id: str, user: dict = Depends(current_user)
) -> dict:
    require_object_id(conversation_id)
    tenant_id = str(user["_id"])
    lead = await get_db().leads.find_one(
        {"_id": ObjectId(conversation_id), "user_id": tenant_id}
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    enqueue(ai_tasks.refresh_conversation_summary, tenant_id, conversation_id, queue="bulk")
    return {"ok": True, "queued": True}


@router.post("/{conversation_id}/extract")
async def rerun_extraction(conversation_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(conversation_id)
    tenant_id = str(user["_id"])
    lead = await get_db().leads.find_one(
        {"_id": ObjectId(conversation_id), "user_id": tenant_id}
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    enqueue(ai_tasks.extract_lead_suggestions, tenant_id, conversation_id, queue="bulk")
    return {"ok": True, "queued": True}


@router.post("/{conversation_id}/reclassify")
async def reclassify(conversation_id: str, user: dict = Depends(current_user)) -> dict:
    require_object_id(conversation_id)
    tenant_id = str(user["_id"])
    lead = await get_db().leads.find_one(
        {"_id": ObjectId(conversation_id), "user_id": tenant_id}
    )
    if not lead:
        raise HTTPException(status_code=404, detail="Not found")
    enqueue(ai_tasks.classify_latest_inbound, tenant_id, conversation_id, "", queue="default")
    return {"ok": True, "queued": True, "last_ai_error_category": lead.get("last_ai_error_category")}
