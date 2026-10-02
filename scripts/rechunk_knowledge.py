"""Re-chunk and re-embed knowledge after a chunking/embedding change.

Normal refreshes skip documents whose text hasn't changed (by design — it saves embedding
cost), so an improvement to chunking needs this one-off pass. For each selected source it
clears the stored "embedded" hash and re-runs ingestion (website sources are re-fetched).

Never run at API startup. Default is dry-run.

Usage:
  python -m scripts.rechunk_knowledge --dry-run [--kb KB_ID]
  python -m scripts.rechunk_knowledge --apply   [--kb KB_ID]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymongo import MongoClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.kb.ingest import run_source  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--kb", help="limit to one knowledge base id")
    args = ap.parse_args()
    db = MongoClient(settings.MONGO_URI)[settings.MONGO_DB]
    q = {"status": {"$nin": ["queued", "processing"]}}
    if args.kb:
        q["kb_id"] = args.kb
    for src in db.kb_sources.find(q):
        sid = str(src["_id"])
        before = db.kb_chunks.count_documents({"source_id": sid})
        if not args.apply:
            print({"source": src.get("title") or src.get("url"), "type": src["type"], "chunks": before, "action": "would re-chunk"})
            continue
        db.kb_documents.update_many({"source_id": sid}, {"$unset": {"embedded_hash": ""}})
        db.kb_sources.update_one({"_id": src["_id"]}, {"$unset": {"crawl_state": "", "run_id": ""}})
        while True:
            out = run_source(db, sid)
            if not out.get("continue"):
                break
        after = db.kb_chunks.count_documents({"source_id": sid})
        print({"source": src.get("title") or src.get("url"), "status": out["status"], "chunks_before": before, "chunks_after": after})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
