from fastapi import APIRouter, Depends

from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.common import serialize

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats")
async def stats(user: dict = Depends(current_user)) -> dict:
    db = get_db()
    uid = str(user["_id"])
    leads_count = await db.leads.count_documents({"user_id": uid})
    agents_count = await db.agents.count_documents({"user_id": uid})
    campaigns_count = await db.campaigns.count_documents({"user_id": uid})
    messages_count = await db.messages.count_documents({"user_id": uid})

    recent_leads_cur = db.leads.find({"user_id": uid}).sort("created_at", -1).limit(5)
    recent_agents_cur = db.agents.find({"user_id": uid}).sort("created_at", -1).limit(3)

    return {
        "counts": {
            "leads": leads_count,
            "agents": agents_count,
            "campaigns": campaigns_count,
            "messages": messages_count,
        },
        "recent_leads": [serialize(d) async for d in recent_leads_cur],
        "recent_agents": [serialize(d) async for d in recent_agents_cur],
    }
