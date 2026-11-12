"""Numbers and names from uploaded lead sheets are cleaned carefully. The formats mirror real lead
exports (floats like 447700900123.0, "+1 447-…-… ext. NN", two numbers in one cell); the names and
numbers are fictional (Ofcom 07700 900xxx drama range)."""
import pytest

from app.services.sheet_contacts import clean_phone, prepare_contacts


@pytest.mark.parametrize("raw, phone, kind", [
    ("+44 7700 900123", "+447700900123", "mobile"),
    ("447700900123.0", "+447700900123", "mobile"),
    (447700900123.0, "+447700900123", "mobile"),
    (447700900123, "+447700900123", "mobile"),
    ("07700900123", "+447700900123", "mobile"),
    ("7700900123", "+447700900123", "mobile"),
    ("0044 7700 900123", "+447700900123", "mobile"),
    ("+44 (0) 7700 900123", "+447700900123", "mobile"),
    ("+44 7700-900-123", "+447700900123", "mobile"),
    ("+1 447-700-9001 ext. 23", "+447700900123", "mobile"),
    ("01632 960123/+447700900123", "+447700900123", "mobile"),
    ("+442079460123", "+442079460123", "landline"),
    ("020 7946 0123", "+442079460123", "landline"),
    ("+1 202-555-0123", "+12025550123", "international"),
    ("+91 98765 43210", "+919876543210", "international"),
    ("4477009001234", None, "invalid"),
    ("hello", None, "invalid"),
    ("", None, "invalid"),
    (None, None, "invalid"),
])
def test_clean_phone(raw, phone, kind):
    got_phone, got_kind, _ = clean_phone(raw)
    assert (got_phone, got_kind) == (phone, kind)


def test_garbled_us_extension_is_repaired_with_a_note():
    assert clean_phone("+1 447-700-9001 ext. 23")[2] == "repaired from a +1 … ext. format"


LEADS_EXPORT = [
    ["Lead Recorded", "Title", "First Name", "Last Name", "Company Name", "E-mail Address", "Mobile No", "Telephone No"],
    ["2026-11-11", "Ms", "Ada", "Lovelace", "Analytical Ltd", "ada@example.com", "+447700900101", ""],
    ["2026-11-11", "Mr", "Alan", "Turing", "Bletchley", "alan@example.com", "447700900102", "447700900"],
    ["2026-11-11", "", "Grace", "Hopper", "Cobol Co", "grace@example.com", "+447700900103", ""],
    ["2026-11-11", "", "Linus", "Torvalds", "Cobol Co", "linus@example.com", "+447700900103", ""],
    ["2026-11-11", "", "", "", "", "", "", ""],
    ["2026-11-11", "", "Edsger", "Dijkstra", "Paths Ltd", "e@example.com", "+442079460104", ""],
    ["2026-11-11", "", "Barbara", "Liskov", "", "", "", "07700900105"],
    ["2026-11-11", "", "No", "Number", "", "x@example.com", "", ""],
]


def test_leads_export_columns_names_and_statuses():
    out = prepare_contacts(LEADS_EXPORT)
    by_name = {c["name"]: c for c in out["contacts"]}
    assert by_name["Ada Lovelace"] == {"row": 2, "name": "Ada Lovelace", "phone": "+447700900101", "raw": "+447700900101", "status": "ok", "note": None}
    assert by_name["Alan Turing"]["phone"] == "+447700900102" and by_name["Alan Turing"]["note"] is None
    assert by_name["Linus Torvalds"]["status"] == "duplicate" and "Grace Hopper" in by_name["Linus Torvalds"]["note"]
    assert by_name["Edsger Dijkstra"]["status"] == "landline"
    assert by_name["Barbara Liskov"]["phone"] == "+447700900105"
    assert by_name["No Number"]["status"] == "invalid"
    assert out["summary"] == {"ok": 4, "duplicate": 1, "landline": 1, "invalid": 2}


def test_single_name_and_phone_number_columns():
    out = prepare_contacts([["Name", "Email", "Company", "Phone Number"], ["Katherine Johnson", "k@example.com", "Orbit", "+44 7700 900106"]])
    assert out["contacts"][0]["name"] == "Katherine Johnson" and out["contacts"][0]["phone"] == "+447700900106"


def test_sample_sheet_layout():
    out = prepare_contacts([["First Name", "Last Name", "Mobile Number"], ["Jane", "Smith", "07700 900123"]])
    assert out["contacts"][0]["name"] == "Jane Smith" and out["contacts"][0]["status"] == "ok"


def test_pasted_numbers_without_a_header():
    out = prepare_contacts([["+447700900001"], ["phone"], ["07700 900002"], ["+447700900001"]])
    assert [(c["phone"], c["status"]) for c in out["contacts"]] == [("+447700900001", "ok"), ("+447700900002", "ok"), ("+447700900001", "duplicate")]
