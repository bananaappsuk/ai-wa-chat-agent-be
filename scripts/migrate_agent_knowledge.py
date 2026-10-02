"""Move each agent's pasted knowledge text into a real knowledge base (retrieval).

For every agent that has `knowledge_base` text and no `knowledge_base_ids`:
  1. create a knowledge base "<agent name> knowledge" for the agent's tenant,
  2. add the text as a text source and index it (chunks + embeddings),
  3. link the agent to it.
The original `knowledge_base` text is left in place (it stays the fallback if retrieval
ever fails). Idempotent: agents already linked are skipped.

Never run at API startup. Default is dry-run.

Usage:
  python -m scripts.migrate_agent_knowledge --dry-run [--tenant USER_ID]
  python -m scripts.migrate_agent_knowledge --apply   [--tenant USER_ID]
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo import MongoClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.kb import store  # noqa: E402
from app.services.kb.ingest import run_source  # noqa: E402


def migrate(db, *, apply: bool, tenant: str | None = None) -> list[dict]:
    q: dict = {"knowledge_base": {"$nin": [None, ""]}, "$or": [{"knowledge_base_ids": {"$exists": False}}, {"knowledge_base_ids": []}]}
    if tenant:
        q["user_id"] = tenant
    report = []
    for agent in db.agents.find(q):
        text = (agent.get("knowledge_base") or "").strip()
        row = {"agent": agent.get("name"), "user_id": agent.get("user_id"), "chars": len(text)}
        if not apply:
            report.append({**row, "action": "would migrate"})
            continue
        now = datetime.now(timezone.utc)
        uid = agent["user_id"]
        kb_id = str(
            db.knowledge_bases.insert_one(
                {
                    "user_id": uid,
                    "name": f"{agent.get('name') or 'Agent'} knowledge"[:100],
                    "description": "Migrated from the agent's pasted knowledge text.",
                    "chunks_to_retrieve": settings.KB_DEFAULT_CHUNKS_TO_RETRIEVE,
                    "similarity_threshold": settings.KB_DEFAULT_SIMILARITY_THRESHOLD,
                    "auto_refresh": True,
                    "created_at": now,
                    "updated_at": now,
                }
            ).inserted_id
        )
        title = f"{agent.get('name') or 'Agent'} — pasted knowledge"[:200]
        sid = str(
            db.kb_sources.insert_one(
                {"user_id": uid, "kb_id": kb_id, "type": "text", "title": title, "status": store.STATUS_QUEUED,
                 "error": None, "stats": {}, "created_at": now, "updated_at": now}
            ).inserted_id
        )
        db.kb_documents.insert_one(
            {"user_id": uid, "kb_id": kb_id, "source_id": sid, "key": "text", "title": title, "text": text,
             "content_hash": hashlib.sha256(text.encode()).hexdigest(), "created_at": now}
        )
        result = run_source(db, sid)
        if result["status"] == store.STATUS_READY:
            db.agents.update_one({"_id": agent["_id"]}, {"$set": {"knowledge_base_ids": [kb_id], "updated_at": now}})
        src = db.kb_sources.find_one({"_id": __import__("bson").ObjectId(sid)})
        report.append({**row, "action": "migrated" if result["status"] == store.STATUS_READY else "FAILED",
                       "kb_id": kb_id, "status": result["status"], "chunks": (src.get("stats") or {}).get("chunks"),
                       "error": src.get("error")})
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--tenant", help="limit to one tenant (user id)")
    args = ap.parse_args()
    db = MongoClient(settings.MONGO_URI)[settings.MONGO_DB]
    if args.apply:
        store.ensure_vector_index(db)
    for row in migrate(db, apply=args.apply, tenant=args.tenant):
        print(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
