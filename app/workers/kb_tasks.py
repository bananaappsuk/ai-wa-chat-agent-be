"""RQ jobs for knowledge-base ingestion and auto-refresh."""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)


def enqueue_ingest(source_id: str):
    from app.workers.queue import enqueue

    return enqueue(
        ingest_kb_source,
        source_id,
        queue="bulk",
        job_timeout=int(settings.KB_INGEST_JOB_TIMEOUT_SECONDS),
    )


def ingest_kb_source(source_id: str) -> dict:
    """One bounded slice of ingestion; re-enqueues itself while a crawl has more to do."""
    from app.services.kb.ingest import run_source
    from app.workers.tasks import _db, _redis

    result = run_source(_db(), source_id, redis=_redis())
    if result.get("continue"):
        enqueue_ingest(source_id)
    return result


def refresh_due_kb_sources() -> int:
    """Queue every website source whose daily auto-refresh is due."""
    from app.services.kb import store
    from app.services.kb.ingest import due_sources
    from app.workers.tasks import _db

    db = _db()
    queued = 0
    for s in due_sources(db):
        res = db.kb_sources.update_one(
            {"_id": s["_id"], "status": {"$nin": [store.STATUS_PROCESSING, store.STATUS_QUEUED]}},
            {"$set": {"status": store.STATUS_QUEUED}},
        )
        if res.modified_count:
            enqueue_ingest(str(s["_id"]))
            queued += 1
    if queued:
        logger.info("kb: queued %s source refreshes", queued)
    return queued
