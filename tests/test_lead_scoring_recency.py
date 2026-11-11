"""HOT means interested *recently*. Regression: contacts who chatted weeks ago (once saying
"interested"/"demo") stayed HOT forever because scores were only computed when they messaged."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import mongomock
from bson import ObjectId

from app.services import lead_scoring
from app.services.lead_scoring import calculate_lead_score

NOW = datetime(2026, 11, 11, 14, 0, tzinfo=timezone.utc)


def _msgs(texts, days_ago):
    return [{"direction": "inbound", "message": t, "created_at": NOW - timedelta(days=days_ago, minutes=i)} for i, t in enumerate(texts)]


ENGAGED = ["Hi", "I'm interested", "can I book a demo?", "how much does it cost", "ok", "thanks"]


def test_engaged_today_is_hot():
    assert calculate_lead_score(inbound_messages=_msgs(ENGAGED, 0), now=NOW)[1] == "hot"


def test_same_chat_a_week_and_a_bit_ago_is_at_most_warm():
    score, label = calculate_lead_score(inbound_messages=_msgs(ENGAGED, 8), now=NOW)
    assert label == "warm" and score <= 69


def test_chat_older_than_the_scoring_window_is_cold():
    assert calculate_lead_score(inbound_messages=_msgs(ENGAGED, 15), now=NOW) == (0, "cold")
    assert calculate_lead_score(inbound_messages=_msgs(ENGAGED * 20, 40), now=NOW) == (0, "cold")


def test_only_recent_messages_count():
    old = _msgs(["interested", "book a demo", "price?", "ok", "yes"], 20)
    recent = _msgs(["hi"], 0)
    _, label = calculate_lead_score(inbound_messages=old + recent, now=NOW)
    assert label == "cold"


def test_keywords_match_whole_words_only():
    facebook = _msgs(["I saw you on facebook", "my border town", "she called"], 0)
    book = _msgs(["I saw you on facebook", "can I book", "my border town"], 0)
    assert calculate_lead_score(inbound_messages=facebook, now=NOW)[0] < calculate_lead_score(inbound_messages=book, now=NOW)[0]
    assert calculate_lead_score(inbound_messages=_msgs(["how much in £?"], 0), now=NOW)[0] > calculate_lead_score(inbound_messages=_msgs(["hello"], 0), now=NOW)[0]


def test_daily_rescore_cools_old_hot_chats_without_moving_them_in_live_chat():
    db = mongomock.MongoClient().db
    uid = str(ObjectId())
    old_updated = datetime(2026, 10, 6, 15, 33)
    stale_hot = db.leads.insert_one({"user_id": uid, "phone": "+918249576382", "name": "Rahul", "score": "hot", "lead_score": 100,
                                     "score_updated_at": datetime(2026, 10, 6), "updated_at": old_updated}).inserted_id
    fresh_hot = db.leads.insert_one({"user_id": uid, "phone": "+447440060335", "name": "Sriram", "score": "hot", "lead_score": 85,
                                     "score_updated_at": datetime.now(timezone.utc)}).inserted_id
    for m in _msgs(ENGAGED, 36):
        db.messages.insert_one({**m, "user_id": uid, "lead_id": str(stale_hot), "created_at": datetime(2026, 10, 6, 15, 30)})
    with patch("app.workers.tasks._db", return_value=db):
        assert lead_scoring.rescore_stale_leads() == 1
    rahul = db.leads.find_one({"_id": stale_hot})
    assert rahul["score"] == "cold" and rahul["updated_at"] == old_updated
    assert db.leads.find_one({"_id": fresh_hot})["score"] == "hot"
