"""B16: canonical E.164 phone normalisation tests."""
from __future__ import annotations

from app.services.phone_norm import normalize_e164, phones_equal


def test_uk_national_leading_zero():
    assert normalize_e164("07700900123") == "+447700900123"


def test_uk_national_with_spaces_and_punctuation():
    assert normalize_e164("07700 900-123") == "+447700900123"
    assert normalize_e164("(0770) 090-0123") == "+447700900123"


def test_already_e164_preserved():
    assert normalize_e164("+447700900123") == "+447700900123"
    assert normalize_e164("+14155238886") == "+14155238886"


def test_whatsapp_prefix_stripped():
    assert normalize_e164("whatsapp:+447700900123") == "+447700900123"
    assert normalize_e164("WhatsApp:+447700900123") == "+447700900123"
    assert normalize_e164("whatsapp:07700900123") == "+447700900123"


def test_bare_country_code_without_plus():
    assert normalize_e164("447700900123") == "+447700900123"


def test_invalid_returns_none():
    assert normalize_e164(None) is None
    assert normalize_e164("") is None
    assert normalize_e164("   ") is None
    assert normalize_e164("abc") is None
    assert normalize_e164("123") is None  # too short
    assert normalize_e164("+1234567890123456789") is None  # too long


def test_phones_equal_normalizes_both_sides():
    assert phones_equal("07700900123", "+447700900123") is True
    assert phones_equal("whatsapp:+447700900123", "447700900123") is True
    assert phones_equal("+447700900123", "+447700900124") is False
    assert phones_equal(None, "+447700900123") is False


def test_default_region_us():
    assert normalize_e164("4155238886", default_region="US") == "+14155238886"
