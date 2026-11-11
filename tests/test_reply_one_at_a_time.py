"""A contact who sends two messages a few seconds apart gets one reply at a time, and an older
message's job steps aside when a newer message has already arrived — never two identical replies.
Regression: "Hi" + "How r u" 4 seconds apart got the same greeting twice (two workers in parallel)."""
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import mongomock
import pytest
from bson import ObjectId

from app.workers import tasks

UID = str(ObjectId())


def _db_with_two_messages():
    db = mongomock.MongoClient().db
    lid = str(db.leads.insert_one({"user_id": UID, "phone": "+447700900111", "name": "Sriram"}).inserted_id)
    t0 = datetime.now(timezone.utc)
    first = str(db.messages.insert_one({"user_id": UID, "lead_id": lid, "direction": "inbound", "message": "Hi", "created_at": t0}).inserted_id)
    second = str(db.messages.insert_one({"user_id": UID, "lead_id": lid, "direction": "inbound", "message": "How r u",
                                         "created_at": t0 + timedelta(seconds=4)}).inserted_id)
    return db, lid, first, second


def test_older_message_steps_aside_for_the_newer_one():
    db, lid, first, second = _db_with_two_messages()
    calls = []
    with patch.object(tasks, "_db", return_value=db), \
         patch.object(tasks, "_generate_and_send_ai_reply", side_effect=lambda *a: calls.append(a[3])):
        tasks.generate_and_send_ai_reply(UID, lid, provider="twilio", trigger_message_id=first)
        tasks.generate_and_send_ai_reply(UID, lid, provider="twilio", trigger_message_id=second)
    assert calls == [second]


def test_no_trigger_still_replies():
    db, lid, _, _ = _db_with_two_messages()
    calls = []
    with patch.object(tasks, "_db", return_value=db), \
         patch.object(tasks, "_generate_and_send_ai_reply", side_effect=lambda *a: calls.append(a)):
        tasks.generate_and_send_ai_reply(UID, lid, provider="twilio")
    assert len(calls) == 1


def _run_concurrently(lead_ids, work_seconds=0.6):
    db = mongomock.MongoClient().db
    spans = {}

    def inner(user_id, lead_id, *_):
        start = time.monotonic()
        time.sleep(work_seconds)
        spans.setdefault(lead_id, []).append((start, time.monotonic()))

    with patch.object(tasks, "_db", return_value=db), patch.object(tasks, "_generate_and_send_ai_reply", side_effect=inner):
        threads = [threading.Thread(target=tasks.generate_and_send_ai_reply, args=(UID, lid)) for lid in lead_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
    return spans


def _overlap(a, b):
    return a[0] < b[1] and b[0] < a[1]


def test_two_replies_to_the_same_contact_never_run_at_once():
    spans = _run_concurrently(["lead-a", "lead-a"])
    a, b = spans["lead-a"]
    assert not _overlap(a, b)


def test_different_contacts_still_reply_in_parallel():
    spans = _run_concurrently(["lead-a", "lead-b"])
    assert _overlap(spans["lead-a"][0], spans["lead-b"][0])


def test_lock_is_released_when_a_reply_fails():
    db = mongomock.MongoClient().db
    with patch.object(tasks, "_db", return_value=db), \
         patch.object(tasks, "_generate_and_send_ai_reply", side_effect=RuntimeError("openai down")):
        with pytest.raises(RuntimeError):
            tasks.generate_and_send_ai_reply(UID, "lead-a")
    assert tasks._redis().get("ai_reply_lock:lead-a") is None


def test_redis_down_still_replies():
    db = mongomock.MongoClient().db
    calls = []
    with patch.object(tasks, "_db", return_value=db), patch.object(tasks, "_redis", side_effect=ConnectionError("redis down")), \
         patch.object(tasks, "_generate_and_send_ai_reply", side_effect=lambda *a: calls.append(a)):
        tasks.generate_and_send_ai_reply(UID, "lead-a")
    assert len(calls) == 1
