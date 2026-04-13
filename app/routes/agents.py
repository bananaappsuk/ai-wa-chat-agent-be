from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.agent import AgentCreate, AgentUpdate
from app.models.common import serialize, utcnow

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("")
async def list_agents(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().agents.find({"user_id": str(user["_id"])}).sort("created_at", -1)
    return [serialize(d) async for d in cur]


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
