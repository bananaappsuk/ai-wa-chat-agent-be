"""Knowledge bases: CRUD, sources (website / crawl / file / text), test playground, gaps."""

import hashlib

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db.mongo import get_db
from app.middleware.auth import current_user
from app.models.common import serialize, utcnow
from app.models.knowledge import KbTestRequest, KnowledgeBaseCreate, KnowledgeBaseUpdate, SourceCreate
from app.services.kb import store

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"])

_SOURCE_PROJECTION = {"crawl_state": 0, "run_id": 0}


def _oid(value: str) -> ObjectId:
    if not ObjectId.is_valid(value):
        raise HTTPException(status_code=404, detail="Not found")
    return ObjectId(value)


async def _get_kb(kb_id: str, user_id: str) -> dict:
    kb = await get_db().knowledge_bases.find_one({"_id": _oid(kb_id), "user_id": user_id})
    if not kb:
        raise HTTPException(status_code=404, detail="Not found")
    return kb


async def _get_source(kb_id: str, source_id: str, user_id: str) -> dict:
    src = await get_db().kb_sources.find_one(
        {"_id": _oid(source_id), "kb_id": kb_id, "user_id": user_id}, _SOURCE_PROJECTION
    )
    if not src:
        raise HTTPException(status_code=404, detail="Not found")
    return src


def _enqueue(source_id: str) -> None:
    from app.workers.kb_tasks import enqueue_ingest

    enqueue_ingest(source_id)


async def _require_room(user: dict, kb_id: str) -> None:
    """Plan allows knowledge chunks, and the KB isn't at its source limit."""
    from app.services.entitlements import require_capacity

    uid = str(user["_id"])
    db = get_db()
    require_capacity(user, "kb_chunks", await db.kb_chunks.count_documents({"user_id": uid}), label="Knowledge chunks")
    if await db.kb_sources.count_documents({"kb_id": kb_id, "user_id": uid}) >= int(settings.KB_MAX_SOURCES_PER_KB):
        raise HTTPException(status_code=400, detail=f"A knowledge base can have at most {settings.KB_MAX_SOURCES_PER_KB} sources.")


async def _kb_counts(user_id: str) -> dict[str, dict]:
    db = get_db()
    out: dict[str, dict] = {}
    async for row in db.kb_sources.aggregate(
        [{"$match": {"user_id": user_id}}, {"$group": {"_id": "$kb_id", "sources": {"$sum": 1}, "processing": {"$sum": {"$cond": [{"$in": ["$status", ["queued", "processing"]]}, 1, 0]}}, "failed": {"$sum": {"$cond": [{"$eq": ["$status", "failed"]}, 1, 0]}}}}]
    ):
        out.setdefault(row["_id"], {}).update(sources=row["sources"], processing=row["processing"], failed=row["failed"])
    async for row in db.kb_chunks.aggregate([{"$match": {"user_id": user_id}}, {"$group": {"_id": "$kb_id", "chunks": {"$sum": 1}}}]):
        out.setdefault(row["_id"], {})["chunks"] = row["chunks"]
    return out


def _kb_out(kb: dict, counts: dict) -> dict:
    out = serialize(kb)
    c = counts.get(out["id"], {})
    out.update(
        source_count=c.get("sources", 0),
        processing_count=c.get("processing", 0),
        failed_count=c.get("failed", 0),
        chunk_count=c.get("chunks", 0),
    )
    return out


# --- Knowledge bases ---------------------------------------------------------------------

@router.get("")
async def list_kbs(user: dict = Depends(current_user)) -> list[dict]:
    uid = str(user["_id"])
    counts = await _kb_counts(uid)
    cur = get_db().knowledge_bases.find({"user_id": uid}).sort("created_at", -1)
    return [_kb_out(kb, counts) async for kb in cur]


@router.post("", status_code=201)
async def create_kb(payload: KnowledgeBaseCreate, user: dict = Depends(current_user)) -> dict:
    from app.services.entitlements import require_capacity

    uid = str(user["_id"])
    db = get_db()
    require_capacity(user, "knowledge_bases", await db.knowledge_bases.count_documents({"user_id": uid}), label="Knowledge bases")
    doc = {**payload.model_dump(), "user_id": uid, "created_at": utcnow(), "updated_at": utcnow()}
    res = await db.knowledge_bases.insert_one(doc)
    doc["_id"] = res.inserted_id
    await run_in_threadpool(_ensure_vector_index)
    return _kb_out(doc, {})


def _ensure_vector_index() -> None:
    from app.workers.tasks import _db

    store.ensure_vector_index(_db())


@router.get("/gaps")
async def list_gaps(limit: int = Query(default=50, ge=1, le=200), user: dict = Depends(current_user)) -> list[dict]:
    """Questions customers asked that no knowledge base could answer (newest first)."""
    cur = get_db().kb_gaps.find({"user_id": str(user["_id"])}).sort("created_at", -1).limit(limit)
    return [serialize(d) async for d in cur]


@router.get("/{kb_id}")
async def get_kb(kb_id: str, user: dict = Depends(current_user)) -> dict:
    uid = str(user["_id"])
    kb = await _get_kb(kb_id, uid)
    return _kb_out(kb, await _kb_counts(uid))


@router.patch("/{kb_id}")
async def update_kb(kb_id: str, payload: KnowledgeBaseUpdate, user: dict = Depends(current_user)) -> dict:
    uid = str(user["_id"])
    await _get_kb(kb_id, uid)
    update = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    update["updated_at"] = utcnow()
    db = get_db()
    await db.knowledge_bases.update_one({"_id": ObjectId(kb_id), "user_id": uid}, {"$set": update})
    if "auto_refresh" in update:
        # Turning auto-refresh off stops scheduled refreshes; on schedules one a day from now.
        from datetime import timedelta

        nxt = utcnow() + timedelta(hours=int(settings.KB_REFRESH_HOURS)) if update["auto_refresh"] else None
        await db.kb_sources.update_many(
            {"kb_id": kb_id, "user_id": uid, "type": {"$in": list(store.REFETCHABLE)}},
            {"$set": {"next_sync_at": nxt}},
        )
    return _kb_out(await _get_kb(kb_id, uid), await _kb_counts(uid))


@router.delete("/{kb_id}", status_code=204)
async def delete_kb(kb_id: str, user: dict = Depends(current_user)) -> None:
    uid = str(user["_id"])
    await _get_kb(kb_id, uid)
    db = get_db()
    await db.kb_chunks.delete_many({"kb_id": kb_id, "user_id": uid})
    await db.kb_documents.delete_many({"kb_id": kb_id, "user_id": uid})
    await db.kb_sources.delete_many({"kb_id": kb_id, "user_id": uid})
    await db.knowledge_bases.delete_one({"_id": ObjectId(kb_id), "user_id": uid})
    await db.agents.update_many({"user_id": uid}, {"$pull": {"knowledge_base_ids": kb_id}})


# --- Sources -----------------------------------------------------------------------------

@router.get("/{kb_id}/sources")
async def list_sources(kb_id: str, user: dict = Depends(current_user)) -> list[dict]:
    uid = str(user["_id"])
    await _get_kb(kb_id, uid)
    cur = get_db().kb_sources.find({"kb_id": kb_id, "user_id": uid}, _SOURCE_PROJECTION).sort("created_at", -1)
    return [serialize(d) async for d in cur]


async def _create_source(user: dict, kb_id: str, doc: dict, documents: list[dict]) -> dict:
    db = get_db()
    now = utcnow()
    doc.update(user_id=str(user["_id"]), kb_id=kb_id, status=store.STATUS_QUEUED, error=None, stats={}, created_at=now, updated_at=now)
    res = await db.kb_sources.insert_one(doc)
    sid = str(res.inserted_id)
    for d in documents:
        await db.kb_documents.insert_one({**d, "user_id": doc["user_id"], "kb_id": kb_id, "source_id": sid, "created_at": now})
    _enqueue(sid)
    await run_in_threadpool(_ensure_vector_index)
    return serialize(await db.kb_sources.find_one({"_id": res.inserted_id}, _SOURCE_PROJECTION))


@router.post("/{kb_id}/sources", status_code=201)
async def add_source(kb_id: str, payload: SourceCreate, user: dict = Depends(current_user)) -> dict:
    uid = str(user["_id"])
    await _get_kb(kb_id, uid)
    await _require_room(user, kb_id)
    if payload.type in ("url", "crawl"):
        from app.services.kb.fetch import FetchError, assert_public_url

        try:
            await run_in_threadpool(assert_public_url, payload.url)
        except FetchError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        doc = {"type": payload.type, "url": payload.url, "title": payload.title or payload.url}
        if payload.type == "crawl":
            doc["crawl"] = (payload.crawl.model_dump() if payload.crawl else {})
        return await _create_source(user, kb_id, doc, [])
    text = (payload.content or "").strip()
    return await _create_source(
        user,
        kb_id,
        {"type": "text", "title": payload.title},
        [{"key": "text", "title": payload.title, "text": text, "content_hash": hashlib.sha256(text.encode()).hexdigest()}],
    )


@router.post("/{kb_id}/sources/upload", status_code=201)
async def upload_source(
    kb_id: str,
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    user: dict = Depends(current_user),
) -> dict:
    from app.services.kb.extract import ExtractError, extract_file

    uid = str(user["_id"])
    await _get_kb(kb_id, uid)
    await _require_room(user, kb_id)
    limit = int(settings.KB_MAX_FILE_MB) * 1_000_000
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=400, detail=f"Files must be {settings.KB_MAX_FILE_MB} MB or smaller.")
    try:
        extracted = await run_in_threadpool(extract_file, data, file.filename or "upload", file.content_type)
    except ExtractError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    name = (title or "").strip()[:200] or extracted.title
    return await _create_source(
        user,
        kb_id,
        {"type": "file", "title": name, "filename": (file.filename or "")[:200], "chars": len(extracted.text)},
        [{"key": "file", "title": name, "text": extracted.text, "content_hash": hashlib.sha256(extracted.text.encode()).hexdigest()}],
    )


@router.post("/{kb_id}/sources/{source_id}/resync")
async def resync_source(kb_id: str, source_id: str, user: dict = Depends(current_user)) -> dict:
    uid = str(user["_id"])
    src = await _get_source(kb_id, source_id, uid)
    if src.get("status") in (store.STATUS_QUEUED, store.STATUS_PROCESSING):
        raise HTTPException(status_code=409, detail="This source is already syncing.")
    db = get_db()
    await db.kb_sources.update_one(
        {"_id": src["_id"]},
        {"$set": {"status": store.STATUS_QUEUED, "error": None, "updated_at": utcnow()}, "$unset": {"crawl_state": "", "run_id": ""}},
    )
    _enqueue(source_id)
    return serialize(await db.kb_sources.find_one({"_id": src["_id"]}, _SOURCE_PROJECTION))


@router.delete("/{kb_id}/sources/{source_id}", status_code=204)
async def delete_source(kb_id: str, source_id: str, user: dict = Depends(current_user)) -> None:
    uid = str(user["_id"])
    src = await _get_source(kb_id, source_id, uid)
    db = get_db()
    await db.kb_chunks.delete_many({"source_id": source_id, "user_id": uid})
    await db.kb_documents.delete_many({"source_id": source_id, "user_id": uid})
    await db.kb_sources.delete_one({"_id": src["_id"]})


@router.get("/{kb_id}/sources/{source_id}/documents")
async def list_documents(kb_id: str, source_id: str, user: dict = Depends(current_user)) -> list[dict]:
    uid = str(user["_id"])
    await _get_source(kb_id, source_id, uid)
    cur = get_db().kb_documents.find(
        {"source_id": source_id, "user_id": uid},
        {"text": 0, "content_hash": 0, "embedded_hash": 0, "last_seen_run": 0},
    ).sort("url", 1)
    return [serialize(d) async for d in cur]


# --- Test playground ---------------------------------------------------------------------

def _run_test(user: dict, kb: dict, question: str, with_answer: bool) -> dict:
    from app.services.ai_config import resolve_ai_settings
    from app.services.ai_prompt import build_system_prompt
    from app.services.ai_provider import chat_completion
    from app.services.kb.embed import EmbeddingError
    from app.services.kb.retrieve import KBContext, search
    from app.workers.tasks import _db

    uid = str(user["_id"])
    kb_id = str(kb["_id"])
    k = int(kb.get("chunks_to_retrieve") or settings.KB_DEFAULT_CHUNKS_TO_RETRIEVE)
    threshold = float(kb.get("similarity_threshold") if kb.get("similarity_threshold") is not None else settings.KB_DEFAULT_SIMILARITY_THRESHOLD)
    try:
        # Search without the threshold so near-misses show too (helps tune the threshold).
        top = search(_db(), user_id=uid, kb_ids=[kb_id], query=question, k=k, threshold=0.0)
    except EmbeddingError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    hits = [h for h in top if h.score >= threshold]
    out = {
        "question": question,
        "threshold": threshold,
        "chunks_to_retrieve": k,
        "results": [{**h.__dict__, "used": h.score >= threshold} for h in top],
        "answer": None,
    }
    if with_answer:
        ai = resolve_ai_settings(user)
        system = build_system_prompt(
            agent={"name": "Knowledge assistant", "kind": "support", "business_description": kb.get("description") or kb.get("name")},
            ai_settings=ai,
            kb_context=KBContext(kb_ids=[kb_id], query=question, searched=True, hits=hits),
        )
        res = chat_completion(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": question}],
            model=ai["model"],
            temperature=0.2,
            max_tokens=int(ai["max_output_tokens"]),
            tenant_id=uid,
            operation="kb_test",
        )
        out["answer"] = res.text if getattr(res, "success", False) else None
    return out


@router.post("/{kb_id}/test")
async def test_kb(kb_id: str, payload: KbTestRequest, user: dict = Depends(current_user)) -> dict:
    kb = await _get_kb(kb_id, str(user["_id"]))
    return await run_in_threadpool(_run_test, user, kb, payload.question.strip(), payload.with_answer)
