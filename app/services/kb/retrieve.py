"""Reply-time retrieval.

1. Skip pure small talk ("hi", "thanks") — nothing to look up.
2. Turn the conversation into a standalone search query (only when there's earlier context).
3. Vector search over this tenant's chunks in the agent's knowledge bases (Atlas
   $vectorSearch; in-process cosine when Atlas Search isn't available).
4. Keep the top-k hits at or above the similarity threshold.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from bson import ObjectId

from app.config import settings
from app.services.kb.embed import EmbeddingError, cosine_score, embed_texts, pack, unpack

logger = logging.getLogger(__name__)

# A message is small talk only if EVERY word is a greeting / pleasantry word — so "hey hi
# how are you" is, but "hi, how much is the course" isn't.
_SMALL_TALK_WORDS = frozenset(
    "hi hii hiii hello helo hey heyy hiya yo hai hola good morning afternoon evening night day "
    "thanks thank thankyou thx ty cheers you u ok okay k cool great nice awesome bye goodbye see ya "
    "later how are r doing is it going what's whats up sup i'm im am fine well there all everyone "
    "dear sir madam mate bro buddy and btw lol haha hehe hmm hmmm wow yay nice cheers gud thx".split()
)


@dataclass
class Hit:
    chunk_id: str
    score: float
    text: str
    heading: str
    title: str
    url: str
    source_id: str
    kb_id: str


@dataclass
class KBContext:
    """What retrieval found for one reply. `searched` is False when we deliberately skipped
    (small talk / no knowledge bases) — the prompt then adds nothing."""

    kb_ids: list[str]
    query: str = ""
    searched: bool = False
    hits: list[Hit] = field(default_factory=list)
    error: Optional[str] = None


def is_small_talk(text: str) -> bool:
    words = re.findall(r"[a-z']+", (text or "").lower())
    return 0 < len(words) <= 8 and all(w in _SMALL_TALK_WORDS for w in words)


def kb_settings(db, user_id: str, kb_ids: list[str]) -> tuple[int, float, list[str]]:
    """Resolve k / threshold across an agent's KBs and drop ids the tenant doesn't own."""
    oids = [ObjectId(k) for k in kb_ids if ObjectId.is_valid(k)]
    kbs = list(db.knowledge_bases.find({"_id": {"$in": oids}, "user_id": user_id}))
    if not kbs:
        return 0, 1.0, []
    k = max(int(kb.get("chunks_to_retrieve") or settings.KB_DEFAULT_CHUNKS_TO_RETRIEVE) for kb in kbs)
    threshold = min(float(kb.get("similarity_threshold") or settings.KB_DEFAULT_SIMILARITY_THRESHOLD) for kb in kbs)
    return max(1, min(10, k)), max(0.0, min(1.0, threshold)), [str(kb["_id"]) for kb in kbs]


def standalone_query(history: list[dict], *, tenant_id: Optional[str] = None) -> str:
    """The customer's latest message, rewritten to stand alone when earlier turns matter."""
    user_turns = [m.get("content") or "" for m in history if m.get("role") == "user"]
    latest = (user_turns[-1] if user_turns else "").strip()
    if len(user_turns) < 2 or len(history) < 2:
        return latest
    convo = "\n".join(f"{m.get('role')}: {(m.get('content') or '')[:500]}" for m in history[-6:])
    try:
        from app.services.ai_provider import chat_completion

        res = chat_completion(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Rewrite the customer's LAST message as one standalone search query for a "
                        "business knowledge base, resolving references like 'it', 'that course', "
                        "'how much' from the conversation. Rules: write the query in English even if "
                        "the customer used another language or mixed languages; if the last message "
                        "isn't asking for information (thanks, ok, bye, small talk), return it "
                        "unchanged; never turn it into an earlier question. Output only the query."
                    ),
                },
                {"role": "user", "content": convo},
            ],
            model="gpt-4o-mini",
            temperature=0.0,
            max_tokens=60,
            tenant_id=tenant_id,
            operation="kb_query_rewrite",
            timeout=8,
            retries=0,  # optional step: if OpenAI is slow, search with the customer's own words
        )
        q = (res.text or "").strip().strip('"') if getattr(res, "success", False) else ""
        return q[:500] or latest
    except Exception:
        return latest


def _vector_search(db, user_id: str, kb_ids: list[str], vector: list[float], k: int) -> list[dict]:
    pipeline = [
        {
            "$vectorSearch": {
                "index": settings.KB_VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": pack(vector),
                "numCandidates": min(500, max(50, k * 20)),
                "limit": k * 2,
                "filter": {"user_id": user_id, "kb_id": {"$in": kb_ids}},
            }
        },
        {"$project": {"embedding": 0, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return list(db.kb_chunks.aggregate(pipeline))


def _scan(db, user_id: str, kb_ids: list[str], vector: list[float], k: int, *, since=None) -> list[dict]:
    q: dict = {"user_id": user_id, "kb_id": {"$in": kb_ids}}
    if since is not None:
        q["created_at"] = {"$gte": since}
    rows = db.kb_chunks.find(q).limit(int(settings.KB_FALLBACK_MAX_CHUNKS))
    scored = []
    for r in rows:
        try:
            r["score"] = cosine_score(vector, unpack(r.get("embedding")))
        except ValueError:
            continue
        r.pop("embedding", None)
        scored.append(r)
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[: k * 2]


def search(
    db,
    *,
    user_id: str,
    kb_ids: list[str],
    query: str,
    k: int,
    threshold: float,
    tenant_id_for_usage: Optional[str] = None,
    extra_queries: Optional[list[str]] = None,
) -> list[Hit]:
    """Top-k chunks for `query` (plus any `extra_queries` — e.g. the customer's own words —
    embedded in the same call; each chunk keeps its best score across the queries)."""
    queries = [q.strip() for q in [query, *(extra_queries or [])] if q and q.strip()]
    queries = list(dict.fromkeys(queries))[:3]
    if not queries or not kb_ids:
        return []
    vectors = embed_texts(queries, tenant_id=tenant_id_for_usage or user_id, operation="kb_query", timeout=10, attempts=2)
    best: dict[str, dict] = {}
    for vector in vectors:
        for r in _rows_for_vector(db, user_id, kb_ids, vector, k):
            key = str(r.get("_id"))
            if key not in best or float(r.get("score") or 0) > float(best[key].get("score") or 0):
                best[key] = r
    rows = sorted(best.values(), key=lambda r: float(r.get("score") or 0), reverse=True)
    return _to_hits(rows, k, threshold)


def _rows_for_vector(db, user_id: str, kb_ids: list[str], vector: list[float], k: int) -> list[dict]:
    from pymongo.errors import PyMongoError

    try:
        rows = _vector_search(db, user_id, kb_ids, vector, k)
        # Atlas indexes new vectors a few seconds after they're written, so knowledge added
        # moments ago would be invisible. Score those fresh chunks directly and merge them in.
        since = datetime.now(timezone.utc) - timedelta(seconds=int(settings.KB_FRESH_SCAN_SECONDS))
        fresh = _scan(db, user_id, kb_ids, vector, k, since=since)
        if fresh:
            by_id = {str(r.get("_id")): r for r in rows}
            for r in fresh:
                cur = by_id.get(str(r.get("_id")))
                if cur is None or float(r["score"]) > float(cur.get("score") or 0):
                    by_id[str(r.get("_id"))] = r
            rows = sorted(by_id.values(), key=lambda r: float(r.get("score") or 0), reverse=True)
    except (PyMongoError, NotImplementedError) as exc:  # no Atlas Search (local Mongo / mongomock)
        logger.debug("kb: $vectorSearch unavailable (%s) — scanning", type(exc).__name__)
        rows = _scan(db, user_id, kb_ids, vector, k)
    return rows


def _to_hits(rows: list[dict], k: int, threshold: float) -> list[Hit]:
    hits: list[Hit] = []
    seen: set[str] = set()
    for r in rows:
        score = float(r.get("score") or 0)
        text = r.get("text") or ""
        sig = text[:200]
        if score < threshold or sig in seen:
            continue
        seen.add(sig)
        hits.append(
            Hit(
                chunk_id=str(r.get("_id")),
                score=round(score, 4),
                text=text,
                heading=r.get("heading") or "",
                title=r.get("title") or "",
                url=r.get("url") or "",
                source_id=str(r.get("source_id") or ""),
                kb_id=str(r.get("kb_id") or ""),
            )
        )
        if len(hits) >= k:
            break
    return hits


def retrieve_for_reply(
    db, *, user_id: str, agent: Optional[dict], history: list[dict], query: Optional[str] = None
) -> Optional[KBContext]:
    """Knowledge for an agent reply. None when the agent has no knowledge bases (callers then
    fall back to the agent's legacy knowledge text). `query` is the router's standalone search
    query; without it the latest message is rewritten here."""
    kb_ids = [str(x) for x in ((agent or {}).get("knowledge_base_ids") or []) if x]
    if not kb_ids:
        return None
    k, threshold, owned = kb_settings(db, user_id, kb_ids)
    ctx = KBContext(kb_ids=owned)
    if not owned:
        return ctx
    # Linked knowledge bases with no content yet must not hide the agent's own pasted knowledge.
    if (agent or {}).get("knowledge_base") and not db.kb_chunks.find_one(
        {"user_id": user_id, "kb_id": {"$in": owned}}, {"_id": 1}
    ):
        return None
    latest = next((m.get("content") or "" for m in reversed(history) if m.get("role") == "user"), "")
    if is_small_talk(latest):
        return ctx
    ctx.query = (query or "").strip() or standalone_query(history, tenant_id=user_id)
    try:
        ctx.hits = search(db, user_id=user_id, kb_ids=owned, query=ctx.query, k=k, threshold=threshold,
                          extra_queries=[latest])
        ctx.searched = True
    except EmbeddingError as exc:
        ctx.error = str(exc)
        logger.warning("kb retrieval failed user_id=%s err=%s", user_id, exc)
    return ctx
