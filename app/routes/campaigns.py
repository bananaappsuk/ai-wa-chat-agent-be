from fastapi import APIRouter, Depends, HTTPException
from bson import ObjectId

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.campaign import CampaignCreate, CampaignUpdate, BlastCreate
from app.models.common import serialize, utcnow
from app.services.twilio_service import to_whatsapp
from app.workers.queue import enqueue
from app.workers import tasks

router = APIRouter(tags=["campaigns"])


@router.get("/campaigns")
async def list_campaigns(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().campaigns.find({"user_id": str(user["_id"])}).sort("created_at", -1)
    return [serialize(d) async for d in cur]


@router.post("/campaigns", status_code=201)
async def create_campaign(payload: CampaignCreate, user: dict = Depends(current_user)) -> dict:
    doc = payload.model_dump()
    doc["user_id"] = str(user["_id"])
    doc["created_at"] = utcnow()
    doc["updated_at"] = utcnow()
    res = await get_db().campaigns.insert_one(doc)
    doc["_id"] = res.inserted_id
    return serialize(doc)


@router.patch("/campaigns/{cid}")
async def update_campaign(cid: str, payload: CampaignUpdate, user: dict = Depends(current_user)) -> dict:
    if not ObjectId.is_valid(cid):
        raise HTTPException(status_code=404, detail="Not found")
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    update["updated_at"] = utcnow()
    res = await get_db().campaigns.update_one(
        {"_id": ObjectId(cid), "user_id": str(user["_id"])}, {"$set": update}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    doc = await get_db().campaigns.find_one({"_id": ObjectId(cid)})
    return serialize(doc)


@router.delete("/campaigns/{cid}", status_code=204)
async def delete_campaign(cid: str, user: dict = Depends(current_user)) -> None:
    if not ObjectId.is_valid(cid):
        raise HTTPException(status_code=404, detail="Not found")
    res = await get_db().campaigns.delete_one(
        {"_id": ObjectId(cid), "user_id": str(user["_id"])}
    )
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")


@router.get("/blasts")
async def list_blasts(user: dict = Depends(current_user)) -> list[dict]:
    cur = get_db().blast_campaigns.find({"user_id": str(user["_id"])}).sort("created_at", -1)
    return [serialize(d) async for d in cur]


@router.post("/blasts", status_code=202)
async def create_blast(payload: BlastCreate, user: dict = Depends(current_user)) -> dict:
    user_id = str(user["_id"])
    db = get_db()
    blast_doc = {
        "user_id": user_id,
        "name": payload.name.strip(),
        "message": payload.message.strip(),
        "total_recipients": len(payload.recipients),
        "sent_count": 0,
        "failed_count": 0,
        "status": "queued",
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    res = await db.blast_campaigns.insert_one(blast_doc)
    blast_id = str(res.inserted_id)
    blast_doc["_id"] = res.inserted_id

    blacklist = {
        d["phone"]
        async for d in db.blacklist.find({"user_id": user_id}, {"phone": 1})
    }

    rows = []
    seen: set[str] = set()
    for raw in payload.recipients:
        try:
            normalized = to_whatsapp(raw).replace("whatsapp:", "")
        except Exception:
            continue
        if normalized in seen or normalized in blacklist:
            continue
        seen.add(normalized)
        rows.append({
            "blast_id": blast_id,
            "phone": normalized,
            "status": "pending",
            "created_at": utcnow(),
        })
    if rows:
        await db.blast_recipients.insert_many(rows)
        await db.blast_campaigns.update_one(
            {"_id": res.inserted_id}, {"$set": {"total_recipients": len(rows)}}
        )

    enqueue(tasks.send_blast_messages, user_id, blast_id)
    fresh = await db.blast_campaigns.find_one({"_id": res.inserted_id})
    return serialize(fresh)


@router.get("/blasts/{bid}/recipients")
async def list_blast_recipients(bid: str, user: dict = Depends(current_user)) -> list[dict]:
    db = get_db()
    if not ObjectId.is_valid(bid):
        raise HTTPException(status_code=404, detail="Not found")
    blast = await db.blast_campaigns.find_one({"_id": ObjectId(bid), "user_id": str(user["_id"])})
    if not blast:
        raise HTTPException(status_code=404, detail="Not found")
    cur = db.blast_recipients.find({"blast_id": bid}).sort("created_at", 1)
    return [serialize(d) async for d in cur]


@router.delete("/blasts/{bid}", status_code=204)
async def delete_blast(bid: str, user: dict = Depends(current_user)) -> None:
    if not ObjectId.is_valid(bid):
        raise HTTPException(status_code=404, detail="Not found")
    db = get_db()
    res = await db.blast_campaigns.delete_one({"_id": ObjectId(bid), "user_id": str(user["_id"])})
    if res.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Not found")
    await db.blast_recipients.delete_many({"blast_id": bid})
