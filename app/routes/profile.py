from fastapi import APIRouter, Depends
from bson import ObjectId

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.user import ProfileUpdate, UserOut
from app.models.common import utcnow, serialize

router = APIRouter(prefix="/profile", tags=["profile"])


def _out(doc: dict) -> UserOut:
    s = serialize(doc)
    return UserOut(
        id=s["id"],
        email=s["email"],
        full_name=s.get("full_name") or "",
        company_name=s.get("company_name"),
        phone=s.get("phone"),
        plan=s.get("plan", "free"),
        role=s.get("role", "user"),
        banned=s.get("banned", False),
        created_at=s.get("created_at"),
    )


@router.get("/me", response_model=UserOut)
async def me(user: dict = Depends(current_user)) -> UserOut:
    return _out(user)


@router.patch("/me", response_model=UserOut)
async def update_me(payload: ProfileUpdate, user: dict = Depends(current_user)) -> UserOut:
    update = {k: v for k, v in payload.model_dump().items() if v is not None}
    if update:
        update["updated_at"] = utcnow()
        await get_db().users.update_one({"_id": ObjectId(user["_id"])}, {"$set": update})
    fresh = await get_db().users.find_one({"_id": ObjectId(user["_id"])})
    return _out(fresh)
