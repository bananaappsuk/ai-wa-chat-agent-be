"""Knowledge-base API: tenant isolation, plan limits, SSRF refusal, sources, agent linking."""
import io
from contextlib import asynccontextmanager

import mongomock
import pytest
from bson import ObjectId
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient

from app.main import app
from app.middleware.auth import current_user
from app.services.kb import ingest as ingest_mod
from app.services.kb import retrieve as retrieve_mod
from tests.kb_helpers import fake_embed, reset_fake_embed

PRO = {"_id": ObjectId(), "plan": "professional", "subscription_status": "active", "role": "user"}
FREE = {"_id": ObjectId(), "plan": "free", "subscription_status": "none", "role": "user"}


@asynccontextmanager
async def _noop_lifespan(_app):
    yield


@pytest.fixture
def env(monkeypatch):
    sync = mongomock.MongoClient()
    adb = AsyncMongoMockClient(mock_mongo_client=sync)["test"]
    sdb = sync["test"]
    enqueued = []
    reset_fake_embed()
    monkeypatch.setattr("app.routes.knowledge.get_db", lambda: adb)
    monkeypatch.setattr("app.routes.agents.get_db", lambda: adb)
    monkeypatch.setattr("app.routes.knowledge._ensure_vector_index", lambda: None)
    monkeypatch.setattr("app.routes.knowledge._enqueue", lambda sid: enqueued.append(sid))
    monkeypatch.setattr("app.services.kb.fetch.assert_public_url", lambda url: url)
    monkeypatch.setattr("app.workers.tasks._db", lambda: sdb)
    monkeypatch.setattr(ingest_mod, "embed_texts", fake_embed)
    monkeypatch.setattr(retrieve_mod, "embed_texts", fake_embed)
    monkeypatch.setattr(ingest_mod, "chunk_cap", lambda db, uid: -1)
    app.router.lifespan_context = _noop_lifespan
    state = {"user": PRO}
    app.dependency_overrides[current_user] = lambda: state["user"]
    with TestClient(app) as c:
        yield {"c": c, "sdb": sdb, "enqueued": enqueued, "state": state}
    app.dependency_overrides.pop(current_user, None)


def _new_kb(c, name="Courses"):
    r = c.post("/api/knowledge-bases", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_list_update_kb(env):
    c = env["c"]
    kb = _new_kb(c)
    assert kb["chunks_to_retrieve"] == 3 and kb["similarity_threshold"] == 0.72 and kb["auto_refresh"] is True
    r = c.patch(f"/api/knowledge-bases/{kb['id']}", json={"chunks_to_retrieve": 5, "similarity_threshold": 0.8})
    assert r.json()["chunks_to_retrieve"] == 5
    assert [k["id"] for k in c.get("/api/knowledge-bases").json()] == [kb["id"]]
    assert c.patch(f"/api/knowledge-bases/{kb['id']}", json={"chunks_to_retrieve": 50}).status_code == 422


def test_free_plan_cannot_create_kb(env):
    env["state"]["user"] = FREE
    r = env["c"].post("/api/knowledge-bases", json={"name": "x"})
    assert r.status_code == 402 and r.json()["detail"]["entitlement"] == "knowledge_bases"


def test_other_tenant_gets_404(env):
    c = env["c"]
    kb = _new_kb(c)
    env["state"]["user"] = {**PRO, "_id": ObjectId()}
    assert c.get(f"/api/knowledge-bases/{kb['id']}").status_code == 404
    assert c.get(f"/api/knowledge-bases/{kb['id']}/sources").status_code == 404
    assert c.post(f"/api/knowledge-bases/{kb['id']}/sources", json={"type": "text", "content": "x"}).status_code == 404
    assert c.delete(f"/api/knowledge-bases/{kb['id']}").status_code == 404


def test_add_text_source_queues_ingest(env):
    c = env["c"]
    kb = _new_kb(c)
    r = c.post(f"/api/knowledge-bases/{kb['id']}/sources", json={"type": "text", "title": "FAQ", "content": "Our workshop fee is £99."})
    assert r.status_code == 201 and r.json()["status"] == "queued"
    assert env["enqueued"] == [r.json()["id"]]
    doc = env["sdb"].kb_documents.find_one({"source_id": r.json()["id"]})
    assert doc["text"] == "Our workshop fee is £99." and doc["user_id"] == str(PRO["_id"])


def test_add_crawl_source_stores_options(env):
    c = env["c"]
    kb = _new_kb(c)
    r = c.post(
        f"/api/knowledge-bases/{kb['id']}/sources",
        json={"type": "crawl", "url": "https://ittalenthub.co.uk/", "crawl": {"max_pages": 30, "exclude_paths": "blog, /privacy"}},
    )
    assert r.status_code == 201, r.text
    src = env["sdb"].kb_sources.find_one({"_id": ObjectId(r.json()["id"])})
    assert src["crawl"]["max_pages"] == 30 and src["crawl"]["exclude_paths"] == ["/blog", "/privacy"]


def test_unsafe_url_rejected(env, monkeypatch):
    from app.services.kb.fetch import FetchError

    def refuse(url):
        raise FetchError("That address isn't allowed.")

    monkeypatch.setattr("app.services.kb.fetch.assert_public_url", refuse)
    kb = _new_kb(env["c"])
    r = env["c"].post(f"/api/knowledge-bases/{kb['id']}/sources", json={"type": "url", "url": "http://169.254.169.254/"})
    assert r.status_code == 400 and "isn't allowed" in r.json()["detail"]


def test_bad_url_and_empty_text_fail_validation(env):
    kb = _new_kb(env["c"])
    base = f"/api/knowledge-bases/{kb['id']}/sources"
    assert env["c"].post(base, json={"type": "url", "url": "ittalenthub.co.uk"}).status_code == 422
    assert env["c"].post(base, json={"type": "text", "content": "   "}).status_code == 422


def test_upload_file_extracts_text(env):
    kb = _new_kb(env["c"])
    r = env["c"].post(
        f"/api/knowledge-bases/{kb['id']}/sources/upload",
        files={"file": ("fees.csv", io.BytesIO(b"Course,Fee\nWorkshop,99\n"), "text/csv")},
    )
    assert r.status_code == 201, r.text
    assert r.json()["type"] == "file" and r.json()["title"] == "fees.csv"
    assert "Course: Workshop; Fee: 99" in env["sdb"].kb_documents.find_one({"source_id": r.json()["id"]})["text"]
    bad = env["c"].post(f"/api/knowledge-bases/{kb['id']}/sources/upload", files={"file": ("x.exe", io.BytesIO(b"MZ"), "application/octet-stream")})
    assert bad.status_code == 400


def test_resync_conflict_while_processing_and_delete(env):
    c, sdb = env["c"], env["sdb"]
    kb = _new_kb(c)
    sid = c.post(f"/api/knowledge-bases/{kb['id']}/sources", json={"type": "text", "content": "abc"}).json()["id"]
    assert c.post(f"/api/knowledge-bases/{kb['id']}/sources/{sid}/resync").status_code == 409  # still queued
    sdb.kb_sources.update_one({"_id": ObjectId(sid)}, {"$set": {"status": "ready"}})
    assert c.post(f"/api/knowledge-bases/{kb['id']}/sources/{sid}/resync").json()["status"] == "queued"
    assert c.delete(f"/api/knowledge-bases/{kb['id']}/sources/{sid}").status_code == 204
    assert sdb.kb_documents.count_documents({"source_id": sid}) == 0


def test_delete_kb_cascades_and_unlinks_agents(env):
    c, sdb = env["c"], env["sdb"]
    kb = _new_kb(c)
    uid = str(PRO["_id"])
    sdb.kb_chunks.insert_one({"user_id": uid, "kb_id": kb["id"], "text": "x"})
    sdb.agents.insert_one({"user_id": uid, "name": "A", "knowledge_base_ids": [kb["id"], "other"]})
    assert c.delete(f"/api/knowledge-bases/{kb['id']}").status_code == 204
    assert sdb.kb_chunks.count_documents({"kb_id": kb["id"]}) == 0
    assert sdb.agents.find_one({"name": "A"})["knowledge_base_ids"] == ["other"]


def test_agent_can_only_link_own_kbs(env):
    c = env["c"]
    kb = _new_kb(c)
    ok = c.post("/api/agents", json={"name": "Workshop", "knowledge_base_ids": [kb["id"]]})
    assert ok.status_code == 201 and ok.json()["knowledge_base_ids"] == [kb["id"]]
    foreign = str(env["sdb"].knowledge_bases.insert_one({"user_id": "someone-else", "name": "theirs"}).inserted_id)
    assert c.post("/api/agents", json={"name": "Sneaky", "knowledge_base_ids": [foreign]}).status_code == 400
    assert c.patch(f"/api/agents/{ok.json()['id']}", json={"knowledge_base_ids": ["not-an-id"]}).status_code == 400


def test_playground_returns_scored_results(env, monkeypatch):
    c, sdb = env["c"], env["sdb"]
    kb = _new_kb(c)
    sid = c.post(f"/api/knowledge-bases/{kb['id']}/sources", json={
        "type": "text", "title": "Workshop", "content": "# Workshop\n\nThe AI weekend workshop fee is ninety nine pounds per participant."}).json()["id"]
    ingest_mod.run_source(sdb, sid)
    r = c.post(f"/api/knowledge-bases/{kb['id']}/test", json={"question": "what is the workshop fee", "with_answer": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["results"] and body["results"][0]["used"] is True
    assert "ninety nine" in body["results"][0]["text"]


def test_gaps_listed_for_tenant_only(env):
    c, sdb = env["c"], env["sdb"]
    sdb.kb_gaps.insert_many([{"user_id": str(PRO["_id"]), "question": "do you offer internships?"},
                             {"user_id": "other", "question": "secret"}])
    assert [g["question"] for g in c.get("/api/knowledge-bases/gaps").json()] == ["do you offer internships?"]
