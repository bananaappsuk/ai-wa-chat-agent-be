"""Ingestion: source → documents → chunks → embeddings → kb_chunks.

Runs in the RQ worker. Each job does a bounded amount of work (one crawl slice) and asks to
be re-enqueued if there's more, so a big crawl never blocks AI replies on the shared worker.

Change detection: every document keeps the hash of the text its chunks were built from, so
a refresh only re-embeds pages that actually changed, and drops pages that disappeared.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from bson import ObjectId

from app.config import settings
from app.services.kb import store
from app.services.kb.chunk import chunk_text, embedding_input
from app.services.kb.crawl import CrawlConfig, CrawlState, crawl_slice
from app.services.kb.embed import EmbeddingError, embed_texts, pack
from app.services.kb.extract import MIN_PAGE_CHARS, ExtractError, extract_response
from app.services.kb.fetch import FetchError, fetch

logger = logging.getLogger(__name__)

SLICE_SECONDS = 40
_LOCK_TTL = 900
JS_SITE_ERROR = (
    "This site's pages have no readable text — it builds its content with JavaScript, which the "
    "crawler doesn't run. Add the content as files or text instead."
)


class LimitReached(Exception):
    pass


@dataclass
class DocIn:
    key: str
    title: str
    text: str
    url: str = ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def chunk_cap(db, user_id: str) -> int:
    """Plan limit on stored chunks (-1 = unlimited)."""
    from app.services.entitlements import effective_entitlements

    user = db.users.find_one({"_id": ObjectId(user_id)}) if ObjectId.is_valid(user_id) else None
    try:
        return int(effective_entitlements(user).get("kb_chunks", 0))
    except (TypeError, ValueError):
        return 0


def ingest_document(db, source: dict, doc: DocIn, run_id: str, cap: int) -> int:
    """(Re)index one document. Returns chunks written (0 when unchanged or too thin)."""
    text = (doc.text or "").strip()
    sid, uid, kb_id = str(source["_id"]), source["user_id"], source["kb_id"]
    if len(text) < MIN_PAGE_CHARS and source["type"] in store.REFETCHABLE:
        return 0
    h = _hash(text)
    existing = db.kb_documents.find_one({"source_id": sid, "key": doc.key})
    if existing and existing.get("embedded_hash") == h:
        db.kb_documents.update_one({"_id": existing["_id"]}, {"$set": {"last_seen_run": run_id}})
        return 0
    chunks = chunk_text(text)
    if not chunks:
        return 0
    if cap >= 0:
        used = store.tenant_chunk_count(db, uid) - int((existing or {}).get("chunk_count") or 0)
        if used + len(chunks) > cap:
            raise LimitReached(f"Your plan's knowledge limit ({cap} chunks) is reached.")
    vectors = embed_texts([embedding_input(doc.title, c) for c in chunks], tenant_id=uid)
    set_doc = {
        "user_id": uid,
        "kb_id": kb_id,
        "source_id": sid,
        "key": doc.key,
        "url": doc.url,
        "title": doc.title[:200],
        "content_hash": h,
        "embedded_hash": h,
        "chars": len(text),
        "chunk_count": len(chunks),
        "last_seen_run": run_id,
        "updated_at": _now(),
    }
    if source["type"] not in store.REFETCHABLE:
        set_doc["text"] = text  # files/snippets can't be re-fetched — keep the text
    res = db.kb_documents.find_one_and_update(
        {"source_id": sid, "key": doc.key},
        {"$set": set_doc, "$setOnInsert": {"created_at": _now()}},
        upsert=True,
        return_document=True,
    )
    doc_id = str(res["_id"])
    db.kb_chunks.delete_many({"doc_id": doc_id})
    db.kb_chunks.insert_many(
        [
            {
                "user_id": uid,
                "kb_id": kb_id,
                "source_id": sid,
                "doc_id": doc_id,
                "index": c.index,
                "heading": c.heading,
                "title": doc.title[:200],
                "url": doc.url,
                "text": c.text,
                "embedding": pack(v),
                "created_at": _now(),
            }
            for c, v in zip(chunks, vectors)
        ]
    )
    return len(chunks)


def _finish(db, source: dict, *, status: str, error: Optional[str], extra_stats: Optional[dict] = None) -> None:
    sid = str(source["_id"])
    agg = list(
        db.kb_documents.aggregate(
            [{"$match": {"source_id": sid}}, {"$group": {"_id": None, "pages": {"$sum": 1}, "chunks": {"$sum": "$chunk_count"}}}]
        )
    )
    totals = agg[0] if agg else {"pages": 0, "chunks": 0}
    kb = db.knowledge_bases.find_one({"_id": ObjectId(source["kb_id"])}) if ObjectId.is_valid(source["kb_id"]) else None
    auto = bool((kb or {}).get("auto_refresh", True)) and source["type"] in store.REFETCHABLE
    now = _now()
    db.kb_sources.update_one(
        {"_id": source["_id"]},
        {
            "$set": {
                "status": status,
                "error": error,
                "stats": {"pages": totals["pages"], "chunks": totals["chunks"], **(extra_stats or {})},
                "last_synced_at": now,
                "next_sync_at": now + timedelta(hours=int(settings.KB_REFRESH_HOURS)) if auto else None,
                "updated_at": now,
            },
            "$unset": {"crawl_state": "", "run_id": ""},
        },
    )


def _drop_unseen(db, source_id: str, run_id: str) -> None:
    """Remove pages that weren't seen in this (complete) run — they no longer exist."""
    stale = [str(d["_id"]) for d in db.kb_documents.find({"source_id": source_id, "last_seen_run": {"$ne": run_id}}, {"_id": 1})]
    if stale:
        db.kb_chunks.delete_many({"doc_id": {"$in": stale}})
        db.kb_documents.delete_many({"_id": {"$in": [ObjectId(x) for x in stale]}})


def _ingest_all(db, source: dict, docs: Iterable[DocIn], run_id: str, cap: int) -> int:
    written = 0
    for d in docs:
        written += ingest_document(db, source, d, run_id, cap)
    return written


def run_source(db, source_id: str, *, redis=None) -> dict:
    """Process one slice of a source. Returns {"status", "continue": bool}."""
    if not ObjectId.is_valid(source_id):
        return {"status": "missing", "continue": False}
    source = db.kb_sources.find_one({"_id": ObjectId(source_id)})
    if not source:
        return {"status": "missing", "continue": False}
    lock_key = f"kb:ingest:{source_id}"
    if redis is not None and not redis.set(lock_key, "1", nx=True, ex=_LOCK_TTL):
        return {"status": "locked", "continue": False}
    try:
        return _run(db, source)
    finally:
        if redis is not None:
            try:
                redis.delete(lock_key)
            except Exception:
                pass


def _run(db, source: dict) -> dict:
    uid, stype = source["user_id"], source["type"]
    cap = chunk_cap(db, uid)
    run_id = source.get("run_id") or str(ObjectId())
    db.kb_sources.update_one(
        {"_id": source["_id"]},
        {"$set": {"status": store.STATUS_PROCESSING, "error": None, "run_id": run_id, "updated_at": _now()}},
    )
    try:
        if stype in ("text", "file"):
            docs = [
                DocIn(key=d["key"], title=d.get("title") or source.get("title") or "", text=d.get("text") or "", url=d.get("url") or "")
                for d in db.kb_documents.find({"source_id": str(source["_id"])})
            ]
            _ingest_all(db, source, docs, run_id, cap)
            _finish(db, source, status=store.STATUS_READY, error=None)
            return {"status": store.STATUS_READY, "continue": False}

        if stype == "url":
            res = fetch(source["url"])
            if res.status != 200:
                raise FetchError(f"The page returned HTTP {res.status}.")
            page = extract_response(res.body, res.content_type, res.url)
            if len(page.text) < MIN_PAGE_CHARS:
                raise ExtractError(JS_SITE_ERROR.replace("This site's pages have", "This page has"))
            _ingest_all(db, source, [DocIn(key=source["url"], title=page.title, text=page.text, url=res.url)], run_id, cap)
            _drop_unseen(db, str(source["_id"]), run_id)
            _finish(db, source, status=store.STATUS_READY, error=None)
            return {"status": store.STATUS_READY, "continue": False}

        # crawl — one bounded slice per job
        c = source.get("crawl") or {}
        cfg = CrawlConfig(
            start_url=source["url"],
            include_paths=c.get("include_paths") or [],
            exclude_paths=c.get("exclude_paths") or [],
            max_pages=int(c.get("max_pages") or settings.KB_CRAWL_DEFAULT_MAX_PAGES),
            max_depth=int(c.get("max_depth") if c.get("max_depth") is not None else 3),
        )
        state = CrawlState.from_dict(source.get("crawl_state"))
        docs = (
            DocIn(key=p.url, title=p.page.title, text=p.page.text, url=p.url)
            for p in crawl_slice(cfg, state, budget_seconds=SLICE_SECONDS)
        )
        try:
            _ingest_all(db, source, docs, run_id, cap)
        except LimitReached as exc:
            _finish(db, source, status=store.STATUS_PARTIAL, error=str(exc), extra_stats=state.stats())
            return {"status": store.STATUS_PARTIAL, "continue": False}
        if not state.done:
            db.kb_sources.update_one(
                {"_id": source["_id"]},
                {"$set": {"crawl_state": state.to_dict(), "stats": state.stats(), "updated_at": _now()}},
            )
            return {"status": store.STATUS_PROCESSING, "continue": True}
        if not db.kb_documents.find_one({"source_id": str(source["_id"]), "last_seen_run": run_id}, {"_id": 1}):
            error = JS_SITE_ERROR if state.thin else "No pages could be read from this site."
            _finish(db, source, status=store.STATUS_FAILED, error=error, extra_stats=state.stats())
            return {"status": store.STATUS_FAILED, "continue": False}
        _drop_unseen(db, str(source["_id"]), run_id)
        _finish(db, source, status=store.STATUS_READY, error=None, extra_stats=state.stats())
        return {"status": store.STATUS_READY, "continue": False}

    except LimitReached as exc:
        _finish(db, source, status=store.STATUS_PARTIAL, error=str(exc))
        return {"status": store.STATUS_PARTIAL, "continue": False}
    except (FetchError, ExtractError, EmbeddingError) as exc:
        _finish(db, source, status=store.STATUS_FAILED, error=str(exc))
        return {"status": store.STATUS_FAILED, "continue": False}
    except Exception as exc:  # never leave a source stuck in "processing"
        logger.exception("kb ingest crashed source=%s", source.get("_id"))
        _finish(db, source, status=store.STATUS_FAILED, error=f"Unexpected error ({type(exc).__name__}).")
        return {"status": store.STATUS_FAILED, "continue": False}


def due_sources(db, limit: int = 20) -> list[dict]:
    """Refetchable sources whose auto-refresh is due."""
    return list(
        db.kb_sources.find(
            {
                "type": {"$in": list(store.REFETCHABLE)},
                "status": {"$nin": [store.STATUS_PROCESSING, store.STATUS_QUEUED]},
                "next_sync_at": {"$ne": None, "$lte": _now()},
            }
        ).limit(limit)
    )
