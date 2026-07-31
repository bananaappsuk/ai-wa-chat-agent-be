"""Unit tests for rule-based lead scoring (C3)."""
from datetime import datetime, timezone, timedelta

from app.services.lead_scoring import calculate_lead_score, map_score_label


def _msg(text: str, hours_ago: float = 0) -> dict:
    return {
        "direction": "inbound",
        "message": text,
        "created_at": datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    }


def test_new_inactive_lead_is_cold():
    score, label = calculate_lead_score(blacklisted=False, inbound_messages=[])
    assert score == 0
    assert label == "cold"


def test_multiple_recent_inbound_increases_score():
    msgs = [_msg("hello", 1), _msg("following up", 1), _msg("still here", 1)]
    score, label = calculate_lead_score(inbound_messages=msgs)
    # 3 * 8 = 24 volume + 25 recency = 49 → warm
    assert score >= 40
    assert label in ("warm", "hot")
    assert score > calculate_lead_score(inbound_messages=[_msg("hi", 1)])[0]


def test_purchase_intent_keywords_increase_score():
    base = calculate_lead_score(inbound_messages=[_msg("hello there", 1)])[0]
    with_intent = calculate_lead_score(
        inbound_messages=[_msg("I want to buy your product", 1)]
    )[0]
    assert with_intent > base


def test_booking_demo_intent_increases_score():
    base = calculate_lead_score(inbound_messages=[_msg("hello there", 1)])[0]
    with_book = calculate_lead_score(
        inbound_messages=[_msg("Can I book a demo call please", 1)]
    )[0]
    assert with_book > base
    assert with_book - base >= 20


def test_opt_out_or_blacklist_is_cold_zero():
    score_bl, label_bl = calculate_lead_score(
        blacklisted=True,
        inbound_messages=[_msg("I want to buy and book a demo", 1)],
    )
    assert score_bl == 0 and label_bl == "cold"

    score_stop, label_stop = calculate_lead_score(
        blacklisted=False,
        inbound_messages=[_msg("please stop messaging me", 1)],
    )
    assert score_stop == 0 and label_stop == "cold"


def test_score_maps_to_hot_warm_cold():
    assert map_score_label(0) == "cold"
    assert map_score_label(39) == "cold"
    assert map_score_label(40) == "warm"
    assert map_score_label(69) == "warm"
    assert map_score_label(70) == "hot"
    assert map_score_label(100) == "hot"

    # Hot path: volume + recency + purchase + budget + booking
    msgs = [
        _msg("hi", 1),
        _msg("interested in pricing", 1),
        _msg("budget is fine, book a demo", 1),
        _msg("I want to buy", 1),
        _msg("call me", 1),
    ]
    score, label = calculate_lead_score(inbound_messages=msgs)
    assert score >= 70
    assert label == "hot"
