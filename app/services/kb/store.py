"""Knowledge-base collections, indexes and the Atlas vector index.

  knowledge_bases  {user_id, name, description, chunks_to_retrieve, similarity_threshold, auto_refresh}
  kb_sources       {user_id, kb_id, type: url|crawl|file|text, title, url, crawl{}, status, error, stats{}, last_synced_at, next_sync_at}
  kb_documents     {user_id, kb_id, source_id, key, url, title, content_hash, chars, chunk_count, text?}
  kb_chunks        {user_id, kb_id, source_id, doc_id, index, heading, title, url, text, embedding}
  kb_gaps          {user_id, agent_id, kb_ids, lead_id, question, created_at}

One vector index (filters: user_id, kb_id) — within the free Atlas tier's 3-index limit.
"""
from __future__ import annotations

import logging

from app.config import settings

logger = logging.getLogger(__name__)

SOURCE_TYPES = ("url", "crawl", "file", "text")
STATUS_QUEUED, STATUS_PROCESSING, STATUS_READY, STATUS_PARTIAL, STATUS_FAILED = (
    "queued",
    "processing",
    "ready",
    "partial",
    "failed",
)
REFETCHABLE = ("url", "crawl")


async def init_kb_indexes(db) -> None:
    """Regular indexes (motor). Called from app startup."""
    await db.knowledge_bases.create_index([("user_id", 1), ("created_at", -1)])
    await db.kb_sources.create_index([("user_id", 1), ("kb_id", 1), ("created_at", -1)])
    await db.kb_sources.create_index([("type", 1), ("next_sync_at", 1)])
    await db.kb_documents.create_index([("source_id", 1), ("key", 1)], unique=True)
    await db.kb_documents.create_index([("kb_id", 1)])
    await db.kb_chunks.create_index([("doc_id", 1)])
    await db.kb_chunks.create_index([("source_id", 1)])
    await db.kb_chunks.create_index([("user_id", 1), ("kb_id", 1)])
    await db.kb_gaps.create_index([("user_id", 1), ("created_at", -1)])


def vector_index_definition() -> dict:
    return {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": int(settings.KB_EMBEDDING_DIMS),
                "similarity": "cosine",
            },
            {"type": "filter", "path": "user_id"},
            {"type": "filter", "path": "kb_id"},
        ]
    }


_vector_index_ok: bool | None = None


def ensure_vector_index(db) -> bool:
    """Create the Atlas vector index if missing (sync pymongo). Returns False when the
    deployment has no Atlas Search (local Mongo) — retrieval then scores in Python.
    Checked once per process."""
    global _vector_index_ok
    if _vector_index_ok is not None:
        return _vector_index_ok
    _vector_index_ok = _ensure_vector_index(db)
    return _vector_index_ok


def _ensure_vector_index(db) -> bool:
    from pymongo.errors import CollectionInvalid, PyMongoError
    from pymongo.operations import SearchIndexModel

    name = settings.KB_VECTOR_INDEX_NAME
    try:
        try:
            db.create_collection("kb_chunks")
        except CollectionInvalid:
            pass
        existing = {ix.get("name") for ix in db.kb_chunks.list_search_indexes()}
        if name not in existing:
            db.kb_chunks.create_search_index(
                SearchIndexModel(name=name, type="vectorSearch", definition=vector_index_definition())
            )
            logger.info("kb: created vector index %s", name)
        return True
    except (PyMongoError, NotImplementedError, TypeError) as exc:
        logger.warning("kb: Atlas vector search unavailable (%s) — using in-process scoring", type(exc).__name__)
        return False


def delete_source_data(db, source_id: str) -> None:
    db.kb_chunks.delete_many({"source_id": source_id})
    db.kb_documents.delete_many({"source_id": source_id})


def delete_kb_data(db, kb_id: str) -> None:
    db.kb_chunks.delete_many({"kb_id": kb_id})
    db.kb_documents.delete_many({"kb_id": kb_id})
    db.kb_sources.delete_many({"kb_id": kb_id})


def tenant_chunk_count(db, user_id: str) -> int:
    return int(db.kb_chunks.count_documents({"user_id": user_id}))
