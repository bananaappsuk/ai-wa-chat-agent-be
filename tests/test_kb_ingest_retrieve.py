"""Knowledge base: ingestion (change detection, limits, crawl refresh) and retrieval."""
from datetime import datetime, timezone

import mongomock
import pytest
from bson import ObjectId

from app.services.kb import ingest as ingest_mod
from app.services.kb import retrieve as retrieve_mod
from app.services.kb.crawl import CrawledPage
from app.services.kb.embed import cosine_score, pack, unpack
from app.services.kb.extract import Extracted
from app.services.kb.ingest import run_source
from app.services.kb.retrieve import is_small_talk, retrieve_for_reply, search
from tests.kb_helpers import fake_embed, fake_vector, reset_fake_embed

WORKSHOP = (
    "# AI Weekend Workshop\n\nA practical introduction to modern AI tools over one weekend, "
    "Saturday and Sunday 10am to 4pm, delivered online with live demonstrations.\n\n"
    "## Fees\n\nThe workshop fee is ninety nine pounds per participant including materials and certificate."
)
DATA_ENG = (
    "# Data Engineering Course\n\nTwo month programme covering SQL, Power BI, Azure Data Factory, "
    "Databricks and data modelling with real projects.\n\n## Price\n\nThe course costs fourteen ninety nine pounds."
)


@pytest.fixture
def db(monkeypatch):
    reset_fake_embed()
    monkeypatch.setattr(ingest_mod, "embed_texts", fake_embed)
    monkeypatch.setattr(retrieve_mod, "embed_texts", fake_embed)
    monkeypatch.setattr(ingest_mod, "chunk_cap", lambda db, uid: -1)
    return mongomock.MongoClient().db


def _kb(db, user="t1", **kw):
    return str(db.knowledge_bases.insert_one({"user_id": user, "name": "KB", "chunks_to_retrieve": 3,
                                              "similarity_threshold": 0.6, "auto_refresh": True, **kw}).inserted_id)


def _text_source(db, kb_id, text, user="t1"):
    sid = str(db.kb_sources.insert_one({"user_id": user, "kb_id": kb_id, "type": "text", "title": "Notes", "status": "queued"}).inserted_id)
    db.kb_documents.insert_one({"user_id": user, "kb_id": kb_id, "source_id": sid, "key": "text", "title": "Notes", "text": text})
    return sid


def test_vectors_pack_roundtrip_and_score():
    v = fake_vector("ai weekend workshop fees")
    assert unpack(pack(v)) == pytest.approx(v, abs=1e-6)
    assert cosine_score(v, v) == pytest.approx(1.0)


def test_text_source_ingests_chunks_with_packed_vectors(db):
    kb = _kb(db)
    sid = _text_source(db, kb, WORKSHOP)
    assert run_source(db, sid)["status"] == "ready"
    chunks = list(db.kb_chunks.find({"source_id": sid}))
    assert chunks and all(c["user_id"] == "t1" and c["kb_id"] == kb for c in chunks)
    assert len(unpack(chunks[0]["embedding"])) == 512
    src = db.kb_sources.find_one({"_id": ObjectId(sid)})
    assert src["stats"]["chunks"] == len(chunks) and src["stats"]["pages"] == 1
    assert src["next_sync_at"] is None  # text snippets don't auto-refresh


def test_unchanged_content_is_not_re_embedded(db):
    kb = _kb(db)
    sid = _text_source(db, kb, WORKSHOP)
    run_source(db, sid)
    calls = fake_embed.calls
    run_source(db, sid)
    assert fake_embed.calls == calls  # same hash → skipped
    db.kb_documents.update_one({"source_id": sid}, {"$set": {"text": WORKSHOP + "\n\nNew: free parking."}})
    run_source(db, sid)
    assert fake_embed.calls == calls + 1  # changed → re-embedded


def test_plan_chunk_limit_marks_source_partial(db, monkeypatch):
    monkeypatch.setattr(ingest_mod, "chunk_cap", lambda db, uid: 0)
    kb = _kb(db)
    sid = _text_source(db, kb, WORKSHOP)
    out = run_source(db, sid)
    src = db.kb_sources.find_one({"_id": ObjectId(sid)})
    assert out["status"] == "partial" and "limit" in src["error"]
    assert db.kb_chunks.count_documents({}) == 0


def test_url_source_failure_is_reported_not_stuck(db, monkeypatch):
    from app.services.kb.fetch import FetchError

    def boom(*a, **k):
        raise FetchError("Couldn't resolve nowhere.test.")

    monkeypatch.setattr(ingest_mod, "fetch", boom)
    kb = _kb(db)
    sid = str(db.kb_sources.insert_one({"user_id": "t1", "kb_id": kb, "type": "url", "url": "https://nowhere.test/", "status": "queued"}).inserted_id)
    assert run_source(db, sid)["status"] == "failed"
    src = db.kb_sources.find_one({"_id": ObjectId(sid)})
    assert src["status"] == "failed" and "resolve" in src["error"]


def test_crawl_refresh_drops_pages_that_disappeared(db, monkeypatch):
    kb = _kb(db)
    sid = str(db.kb_sources.insert_one({"user_id": "t1", "kb_id": kb, "type": "crawl", "url": "https://site.test/",
                                        "crawl": {"max_pages": 10}, "status": "queued"}).inserted_id)
    site = {"https://site.test/a": WORKSHOP, "https://site.test/b": DATA_ENG}

    def fake_slice(cfg, state, **kw):
        for url, text in list(site.items()):
            state.fetched += 1
            yield CrawledPage(url=url, page=Extracted(title=url, text=text))
        state.stopped_reason, state.done = "complete", True

    monkeypatch.setattr(ingest_mod, "crawl_slice", fake_slice)
    assert run_source(db, sid)["status"] == "ready"
    assert db.kb_documents.count_documents({"source_id": sid}) == 2
    src = db.kb_sources.find_one({"_id": ObjectId(sid)})
    assert src["next_sync_at"] is not None  # websites auto-refresh
    del site["https://site.test/b"]  # page removed from the site
    run_source(db, sid)
    assert db.kb_documents.count_documents({"source_id": sid}) == 1
    assert not db.kb_chunks.find_one({"url": "https://site.test/b"})


def test_crawl_in_progress_asks_to_continue(db, monkeypatch):
    kb = _kb(db)
    sid = str(db.kb_sources.insert_one({"user_id": "t1", "kb_id": kb, "type": "crawl", "url": "https://site.test/", "crawl": {}, "status": "queued"}).inserted_id)

    def half(cfg, state, **kw):
        state.fetched += 1
        state.frontier = [["https://site.test/next", 1]]
        yield CrawledPage(url="https://site.test/a", page=Extracted(title="A", text=WORKSHOP))

    monkeypatch.setattr(ingest_mod, "crawl_slice", half)
    out = run_source(db, sid)
    assert out == {"status": "processing", "continue": True}
    assert db.kb_sources.find_one({"_id": ObjectId(sid)})["crawl_state"]["frontier"] == [["https://site.test/next", 1]]


def test_ingest_lock_prevents_parallel_runs(db):
    import fakeredis

    r = fakeredis.FakeStrictRedis()
    kb = _kb(db)
    sid = _text_source(db, kb, WORKSHOP)
    r.set(f"kb:ingest:{sid}", "1")
    assert run_source(db, sid, redis=r)["status"] == "locked"


def test_search_finds_relevant_chunk_and_isolates_tenants(db):
    kb_a = _kb(db, user="t1")
    kb_b = _kb(db, user="t2")
    run_source(db, _text_source(db, kb_a, WORKSHOP, user="t1"))
    run_source(db, _text_source(db, kb_b, DATA_ENG, user="t2"))
    hits = search(db, user_id="t1", kb_ids=[kb_a], query="how much is the workshop fee", k=3, threshold=0.5)
    assert hits and "ninety nine" in " ".join(h.text for h in hits)
    # tenant t1 can't reach t2's knowledge even by passing its KB id
    assert search(db, user_id="t1", kb_ids=[kb_b], query="data engineering course price", k=3, threshold=0.0) == []


def test_threshold_filters_weak_matches(db):
    kb = _kb(db)
    run_source(db, _text_source(db, kb, WORKSHOP))
    assert search(db, user_id="t1", kb_ids=[kb], query="quantum chromodynamics lattice", k=3, threshold=0.6) == []


@pytest.mark.parametrize("text,small", [
    ("hi", True), ("Hey hi  how are you", True), ("thanks!", True), ("good morning", True),
    ("how much is the course", False), ("hi, tell me about the data engineering course", False),
    ("hi how much is it", False), ("ok what are the timings", False),
])
def test_small_talk_detection(text, small):
    assert is_small_talk(text) is small


def test_retrieve_for_reply(db):
    kb = _kb(db)
    run_source(db, _text_source(db, kb, WORKSHOP))
    agent = {"_id": ObjectId(), "knowledge_base_ids": [kb]}
    # small talk → no lookup
    ctx = retrieve_for_reply(db, user_id="t1", agent=agent, history=[{"role": "user", "content": "hello"}])
    assert ctx is not None and ctx.searched is False
    # real question → hits
    ctx = retrieve_for_reply(db, user_id="t1", agent=agent, history=[{"role": "user", "content": "what is the workshop fee per participant"}])
    assert ctx.searched and ctx.hits
    # agent without KBs → None (legacy knowledge text path)
    assert retrieve_for_reply(db, user_id="t1", agent={"knowledge_base_ids": []}, history=[]) is None
    # another tenant's KB id is ignored
    ctx = retrieve_for_reply(db, user_id="t2", agent=agent, history=[{"role": "user", "content": "workshop fee"}])
    assert ctx.kb_ids == [] and not ctx.hits


def test_due_sources_only_returns_due_refetchable(db):
    from datetime import timedelta

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    future = datetime.now(timezone.utc) + timedelta(hours=5)
    db.kb_sources.insert_many([
        {"type": "crawl", "status": "ready", "next_sync_at": past, "tag": "due"},
        {"type": "url", "status": "ready", "next_sync_at": future, "tag": "later"},
        {"type": "crawl", "status": "processing", "next_sync_at": past, "tag": "busy"},
        {"type": "text", "status": "ready", "next_sync_at": past, "tag": "text"},
        {"type": "url", "status": "ready", "next_sync_at": None, "tag": "off"},
    ])
    assert [s["tag"] for s in ingest_mod.due_sources(db)] == ["due"]


def test_javascript_only_site_fails_with_clear_message(db, monkeypatch):
    kb = _kb(db)
    sid = str(db.kb_sources.insert_one({"user_id": "t1", "kb_id": kb, "type": "crawl", "url": "https://spa.test/", "crawl": {}, "status": "queued"}).inserted_id)

    def spa(cfg, state, **kw):  # every page is an empty JS shell
        state.fetched, state.thin = 1, 1
        state.stopped_reason, state.done = "complete", True
        return iter(())

    monkeypatch.setattr(ingest_mod, "crawl_slice", spa)
    assert run_source(db, sid)["status"] == "failed"
    assert "JavaScript" in db.kb_sources.find_one({"_id": ObjectId(sid)})["error"]
