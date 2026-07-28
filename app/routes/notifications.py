from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.security.validation import require_object_id
from app.services import notifications as notif_svc

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
@router.get("/", include_in_schema=False)
async def list_notifications(
    user: dict = Depends(current_user),
    page: int = Query(default=1, ge=1, le=10_000),
    page_size: int = Query(default=25, ge=1, le=100),
    unread: bool | None = Query(default=None),
) -> dict:
    return await notif_svc.list_notifications(
        get_db(),
        user_id=str(user["_id"]),
        page=page,
        page_size=page_size,
        unread_only=bool(unread),
    )


@router.get("/unread-count")
async def get_unread_count(user: dict = Depends(current_user)) -> dict:
    n = await notif_svc.unread_count(get_db(), user_id=str(user["_id"]))
    return {"count": n}


@router.post("/{notification_id}/read")
async def mark_one_read(
    notification_id: str, user: dict = Depends(current_user)
) -> dict:
    require_object_id(notification_id)
    ok = await notif_svc.mark_read(
        get_db(), user_id=str(user["_id"]), notification_id=notification_id
    )
    if not ok:
        # Idempotent: already read or missing — verify ownership
        from bson import ObjectId

        doc = await get_db().notifications.find_one(
            {"_id": ObjectId(notification_id), "user_id": str(user["_id"])}
        )
        if not doc:
            raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@router.post("/read-all")
async def mark_all(user: dict = Depends(current_user)) -> dict:
    n = await notif_svc.mark_all_read(get_db(), user_id=str(user["_id"]))
    return {"ok": True, "updated": n}


@router.delete("/{notification_id}")
async def delete_one(
    notification_id: str, user: dict = Depends(current_user)
) -> dict:
    require_object_id(notification_id)
    ok = await notif_svc.delete_notification(
        get_db(), user_id=str(user["_id"]), notification_id=notification_id
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}
