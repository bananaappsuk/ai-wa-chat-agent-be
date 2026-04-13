from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId
from pydantic import BaseModel

from app.db.mongo import get_db
from app.middleware.auth import current_admin
from app.models.common import serialize, utcnow

router = APIRouter(prefix="/admin", tags=["admin"])


class RoleBody(BaseModel):
    role: str


class PlanBody(BaseModel):
    plan: str


@router.get("/users")
async def list_users(_: dict = Depends(current_admin)) -> list[dict]:
    cur = get_db().users.find({}, {"password_hash": 0}).sort("created_at", -1)
    out = []
    async for u in cur:
        s = serialize(u)
        s["roles"] = [s.get("role", "user")]
        out.append(s)
    return out


@router.post("/users/{uid}/ban")
async def ban_user(uid: str, _: dict = Depends(current_admin)) -> dict:
    if not ObjectId.is_valid(uid):
        raise HTTPException(status_code=404, detail="Not found")
    await get_db().users.update_one({"_id": ObjectId(uid)}, {"$set": {"banned": True, "updated_at": utcnow()}})
    return {"ok": True}


@router.post("/users/{uid}/unban")
async def unban_user(uid: str, _: dict = Depends(current_admin)) -> dict:
    if not ObjectId.is_valid(uid):
        raise HTTPException(status_code=404, detail="Not found")
    await get_db().users.update_one({"_id": ObjectId(uid)}, {"$set": {"banned": False, "updated_at": utcnow()}})
    return {"ok": True}


@router.delete("/users/{uid}", status_code=204)
async def delete_user(uid: str, admin: dict = Depends(current_admin)) -> None:
    if not ObjectId.is_valid(uid):
        raise HTTPException(status_code=404, detail="Not found")
    if str(admin["_id"]) == uid:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db = get_db()
    await db.users.delete_one({"_id": ObjectId(uid)})
    await db.leads.delete_many({"user_id": uid})
    await db.messages.delete_many({"user_id": uid})
    await db.agents.delete_many({"user_id": uid})
    await db.campaigns.delete_many({"user_id": uid})
    await db.blast_campaigns.delete_many({"user_id": uid})
    await db.blacklist.delete_many({"user_id": uid})


@router.post("/users/{uid}/role")
async def update_role(uid: str, body: RoleBody, _: dict = Depends(current_admin)) -> dict:
    if body.role not in ("user", "moderator", "admin"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if not ObjectId.is_valid(uid):
        raise HTTPException(status_code=404, detail="Not found")
    await get_db().users.update_one(
        {"_id": ObjectId(uid)}, {"$set": {"role": body.role, "updated_at": utcnow()}}
    )
    return {"ok": True}


@router.post("/users/{uid}/plan")
async def update_plan(uid: str, body: PlanBody, _: dict = Depends(current_admin)) -> dict:
    if not ObjectId.is_valid(uid):
        raise HTTPException(status_code=404, detail="Not found")
    await get_db().users.update_one(
        {"_id": ObjectId(uid)}, {"$set": {"plan": body.plan, "updated_at": utcnow()}}
    )
    return {"ok": True}
