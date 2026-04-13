from fastapi import APIRouter, Depends, HTTPException

from app.middleware.auth import current_user
from app.models.lead import LeadCreate, LeadUpdate
from app.models.common import serialize
from app.services import lead_service

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("")
async def list_leads(user: dict = Depends(current_user)) -> list[dict]:
    docs = await lead_service.list_leads(str(user["_id"]))
    return [serialize(d) for d in docs]


@router.post("", status_code=201)
async def create_lead(payload: LeadCreate, user: dict = Depends(current_user)) -> dict:
    doc = await lead_service.create_lead(str(user["_id"]), payload.model_dump())
    return serialize(doc)


@router.get("/{lead_id}")
async def get_lead(lead_id: str, user: dict = Depends(current_user)) -> dict:
    doc = await lead_service.get_lead(str(user["_id"]), lead_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return serialize(doc)


@router.patch("/{lead_id}")
async def update_lead(lead_id: str, payload: LeadUpdate, user: dict = Depends(current_user)) -> dict:
    doc = await lead_service.update_lead(str(user["_id"]), lead_id, payload.model_dump(exclude_unset=True))
    if not doc:
        raise HTTPException(status_code=404, detail="Not found")
    return serialize(doc)


@router.delete("/{lead_id}", status_code=204)
async def delete_lead(lead_id: str, user: dict = Depends(current_user)) -> None:
    ok = await lead_service.delete_lead(str(user["_id"]), lead_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Not found")
