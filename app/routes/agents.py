from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from bson import ObjectId

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.agent import AgentCreate, AgentUpdate
from app.models.common import serialize, utcnow
from app.services.knowledge_extract import extract_knowledge_text

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("")
async def list_agents(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().agents.find({"user_id": str(user["_id"])}).sort("created_at", -1)
    return [serialize(d) async for d in cur]


@router.get("/options")
async def agent_campaign_options(user: dict = Depends(current_user)) -> list[dict]:
    """Tenant-safe active agents usable for AI Agent Campaigns (no prompts/secrets)."""
    from app.security.permissions import require_permission

    require_permission(user, "agents.use_in_campaigns")
    user_id = str(user["_id"])
    cur = (
        get_db()
        .agents.find({"user_id": user_id, "status": "active"})
        .sort("updated_at", -1)
    )
    out: list[dict] = []
    async for d in cur:
        enabled = d.get("campaign_enabled")
        if enabled is False:
            continue
        out.append(
            {
                "id": str(d["_id"]),
                "name": d.get("name"),
                "kind": d.get("kind") or "inbound",
                "description": (d.get("prompt") or "")[:160] or None,
                "tone": d.get("tone") or "neutral",
                "language": None,
                "enabled": True,
                "campaign_capable": True,
                "campaign_enabled": True,
                "status": d.get("status"),
            }
        )
    return out


@router.post("/extract-knowledge")
async def extract_knowledge(
    file: UploadFile = File(...),
    user: dict = Depends(current_user),
) -> dict:
    """Extract plain text from an uploaded knowledge file (txt/md/csv/pdf)."""
    _ = user
    data = await file.read()
    try:
        text, label = extract_knowledge_text(
            data=data,
            filename=file.filename or "upload",
            content_type=file.content_type,
            max_chars=10000,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"filename": label, "text": text, "chars": len(text)}


@router.post("", status_code=201)
async def create_agent(payload: AgentCreate, user: dict = Depends(current_user)) -> dict:
    doc = payload.model_dump()
    doc["user_id"] = str(user["_id"])
    doc["created_at"] = utcnow()
    doc["updated_at"] = utcnow()
    res = await get_db().agents.insert_one(doc)
    doc["_id"] = res.inserted_id
    return serialize(doc)


@router.patch("/{agent_id}")
async def update_agent(agent_id: str, payload: AgentUpdate, user: dict = Depends(current_user)) -> dict:
    if not ObjectId.is_valid(agent_id):
        raise HTTPException(status_code=404, detail="Not found")
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    update["updated_at"] = utcnow()
    res = await get_db().agents.update_one(
        {"_id": ObjectId(agent_id), "user_id": str(user["_id"])}, {"$set": update}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db().agents.find_one({"_id": ObjectId(agent_id)})
    return serialize(doc)


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, user: dict = Depends(current_user)) -> None:
    if not ObjectId.is_valid(agent_id):
        raise HTTPException(status_code=404, detail="Not found")
    res = await get_db().agents.delete_one(
        {"_id": ObjectId(agent_id), "user_id": str(user["_id"])}
    )
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
