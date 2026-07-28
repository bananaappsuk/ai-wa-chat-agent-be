"""Safe bulk lead operations (no bulk opt-in)."""
from __future__ import annotations

from typing import Any, Optional

from bson import ObjectId
from fastapi import HTTPException

from app.config import settings
from app.models.common import serialize, utcnow
from app.security.audit import audit
from app.security.validation import require_object_id
from app.services.consent_ops import apply_consent_change
from app.services.ws_manager import ws_manager

ALLOWED_BULK_ACTIONS = frozenset(
    {
        "assign_agent",
        "pause_ai",
        "resume_ai",
        "mark_needs_human",
        "clear_needs_human",
        "add_tag",
        "remove_tag",
        "opt_out",
        "add_to_blacklist",
    }
)


async def run_bulk_action(
    db,
    *,
    user_id: str,
    lead_ids: list[str],
    action: str,
    value: Any = None,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    action = (action or "").strip()
    if action not in ALLOWED_BULK_ACTIONS:
        raise HTTPException(status_code=400, detail="Unsupported bulk action")
    if action in ("opt_in", "bulk_opt_in"):
        raise HTTPException(status_code=400, detail="Bulk opt-in is not allowed")

    max_items = max(1, int(settings.LEAD_BULK_MAX_ITEMS))
    if not lead_ids:
        raise HTTPException(status_code=400, detail="lead_ids required")
    if len(lead_ids) > max_items:
        raise HTTPException(status_code=400, detail=f"Maximum {max_items} lead_ids per request")

    oids: list[ObjectId] = []
    failed = 0
    for lid in lead_ids:
        try:
            oids.append(require_object_id(lid))
        except HTTPException:
            failed += 1

    affected = 0
    skipped = 0
    now = utcnow()

    from app.services.lead_scoring import recalculate_lead_score

    for oid in oids:
        lead = await db.leads.find_one({"_id": oid, "user_id": user_id})
        if not lead:
            skipped += 1
            continue
        lead_id = str(oid)
        try:
            if action == "assign_agent":
                agent_id = str(value or "").strip()
                if not agent_id or len(agent_id) > 64:
                    failed += 1
                    continue
                await db.leads.update_one(
                    {"_id": oid, "user_id": user_id},
                    {"$set": {"assigned_agent_id": agent_id, "updated_at": now}},
                )
            elif action == "pause_ai":
                await db.leads.update_one(
                    {"_id": oid, "user_id": user_id},
                    {"$set": {"ai_paused": True, "updated_at": now}},
                )
            elif action == "resume_ai":
                await db.leads.update_one(
                    {"_id": oid, "user_id": user_id},
                    {"$set": {"ai_paused": False, "updated_at": now}},
                )
            elif action == "mark_needs_human":
                await db.leads.update_one(
                    {"_id": oid, "user_id": user_id},
                    {"$set": {"needs_human": True, "updated_at": now}},
                )
            elif action == "clear_needs_human":
                await db.leads.update_one(
                    {"_id": oid, "user_id": user_id},
                    {"$set": {"needs_human": False, "updated_at": now}},
                )
            elif action == "add_tag":
                tag = str(value or "").strip()[:40]
                if not tag:
                    failed += 1
                    continue
                await db.leads.update_one(
                    {"_id": oid, "user_id": user_id},
                    {"$addToSet": {"tags": tag}, "$set": {"updated_at": now}},
                )
                await recalculate_lead_score(user_id, lead_id)
            elif action == "remove_tag":
                tag = str(value or "").strip()[:40]
                if not tag:
                    failed += 1
                    continue
                await db.leads.update_one(
                    {"_id": oid, "user_id": user_id},
                    {"$pull": {"tags": tag}, "$set": {"updated_at": now}},
                )
                await recalculate_lead_score(user_id, lead_id)
            elif action == "opt_out":
                await apply_consent_change(
                    db,
                    user_id=user_id,
                    lead_id=lead_id,
                    status="opted_out",
                    source="manual",
                    reason=str(value or "bulk_opt_out")[:200],
                    changed_by=user_id,
                    phone=lead.get("phone"),
                    request_id=request_id,
                )
                await recalculate_lead_score(user_id, lead_id)
            elif action == "add_to_blacklist":
                phone = lead.get("phone")
                if not phone:
                    skipped += 1
                    continue
                await apply_consent_change(
                    db,
                    user_id=user_id,
                    lead_id=lead_id,
                    status="opted_out",
                    source="blacklist",
                    reason=str(value or "bulk_blacklist")[:200],
                    changed_by=user_id,
                    phone=phone,
                    request_id=request_id,
                )
                await recalculate_lead_score(user_id, lead_id)
            else:
                failed += 1
                continue

            affected += 1
            updated = await db.leads.find_one({"_id": oid, "user_id": user_id})
            if updated:
                await ws_manager.push(user_id, "lead:updated", serialize(updated))
        except Exception:
            failed += 1

    audit(
        f"leads.bulk.{action}",
        user_id=user_id,
        request_id=request_id,
    )
    return {
        "action": action,
        "requested": len(lead_ids),
        "affected": affected,
        "skipped": skipped,
        "failed": failed,
    }
