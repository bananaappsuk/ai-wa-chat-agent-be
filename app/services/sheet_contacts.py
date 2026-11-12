"""Read contacts (name + phone) from an uploaded sheet's rows and clean each number."""
from __future__ import annotations

import re
from typing import Any, Optional

_FIRST = ("first name", "firstname", "first", "given name", "forename")
_LAST = ("last name", "lastname", "last", "surname", "family name")
_FULL = ("name", "full name", "fullname", "contact name")
_MOBILE = ("mobile number", "mobile no", "mobile", "mobile phone", "whatsapp", "whatsapp number", "cell", "cell phone")
_PHONE = ("phone number", "phone", "phone no", "telephone", "telephone no", "telephone number", "tel", "number", "contact number")

_US_EXT_UK = re.compile(r"^\+1\s*447[\s.-]?(\d{3})[\s.-]?(\d{4})\s*(?:ext\.?|x)\s*(\d{2})$", re.I)
_SPLIT = re.compile(r"\s*(?:/|,|;|\||\bor\b)\s*", re.I)


def _key(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower().rstrip(":.")


def _cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    s = str(v).strip()
    return s[:-2] if re.fullmatch(r"\d+\.0", s) else s


def detect_columns(header: list[Any]) -> dict[str, Optional[int]]:
    names = [_key(h) for h in header]

    def find(options: tuple[str, ...]) -> Optional[int]:
        for opt in options:
            if opt in names:
                return names.index(opt)
        return None

    return {
        "first": find(_FIRST),
        "last": find(_LAST),
        "full": find(_FULL),
        "mobile": find(_MOBILE),
        "phone": find(_PHONE),
    }


def _one_number(raw: str) -> tuple[Optional[str], str, Optional[str]]:
    m = _US_EXT_UK.match(raw.strip())
    if m:
        return f"+447{m.group(1)}{m.group(2)}{m.group(3)}", "mobile", "repaired from a +1 … ext. format"
    s = re.split(r"\s*(?:ext\.?|x)\s*\d+\s*$", raw.strip(), flags=re.I)[0]
    s = s.replace("(0)", "")
    plus = s.lstrip().startswith("+")
    d = re.sub(r"\D", "", s)
    if not d:
        return None, "invalid", None
    if not plus:
        if d.startswith("00"):
            d = d[2:]
        elif d.startswith("0") and len(d) == 11:
            d = "44" + d[1:]
        elif d.startswith("7") and len(d) == 10:
            d = "44" + d
        elif not (d.startswith("44") and len(d) == 12):
            return None, "invalid", None
    if d.startswith("440") and len(d) == 13:
        d = "44" + d[3:]
    if d.startswith("44"):
        if re.fullmatch(r"447\d{9}", d):
            return f"+{d}", "mobile", None
        if re.fullmatch(r"44[1-3]\d{8,9}", d):
            return f"+{d}", "landline", None
        return None, "invalid", None
    if 8 <= len(d) <= 15:
        return f"+{d}", "international", None
    return None, "invalid", None


def clean_phone(raw: Any) -> tuple[Optional[str], str, Optional[str]]:
    """Return (E.164 phone or None, kind, note). kind: mobile | landline | international | invalid.
    A cell holding two numbers ("01223 789606 / +447866420578") yields the UK mobile."""
    text = _cell(raw)
    if not text:
        return None, "invalid", None
    parts = [p for p in _SPLIT.split(text) if p.strip()] or [text]
    found = [_one_number(p) for p in parts]
    for rank in ("mobile", "landline", "international"):
        for phone, kind, note in found:
            if kind == rank:
                if len(parts) > 1:
                    note = note or "picked from a cell with more than one number"
                return phone, kind, note
    return None, "invalid", None


def _name(row: list[Any], cols: dict[str, Optional[int]]) -> str:
    def at(i: Optional[int]) -> str:
        return _cell(row[i]) if i is not None and i < len(row) else ""

    full = " ".join(x for x in (at(cols["first"]), at(cols["last"])) if x)
    return re.sub(r"\s+", " ", full or at(cols["full"])).strip()[:100]


def prepare_contacts(rows: list[list[Any]]) -> dict[str, Any]:
    """Rows as read from a sheet (first row may be a header). With no recognisable header,
    every cell that looks like a phone number is read."""
    rows = [list(r or []) for r in rows if any(_cell(c) for c in (r or []))]
    if not rows:
        return {"columns": {}, "contacts": [], "summary": {}}
    cols = detect_columns(rows[0])
    has_header = cols["mobile"] is not None or cols["phone"] is not None
    contacts: list[dict[str, Any]] = []
    seen: dict[str, str] = {}
    body = rows[1:] if has_header else rows
    first_row_no = 2 if has_header else 1
    for offset, row in enumerate(body):
        row_no = first_row_no + offset
        if has_header:
            picked = [_cell(row[i]) for i in (cols["mobile"], cols["phone"]) if i is not None and i < len(row) and _cell(row[i])]
            name = _name(row, cols)
            if not picked:
                contacts.append({"row": row_no, "name": name, "phone": None, "raw": "", "status": "invalid", "note": "no phone number"})
                continue
            tried = [(c, *clean_phone(c)) for c in picked]
            best = next((t for rank in ("mobile", "landline", "international") for t in tried if t[2] == rank), tried[0])
            candidates = [best]
        else:
            name = ""
            candidates = [(_cell(c), *clean_phone(c)) for c in row if re.search(r"\d{7,}", re.sub(r"\D", "", _cell(c)))]
        for raw, phone, kind, note in candidates:
            status = "ok" if kind == "mobile" else kind
            if phone and phone in seen:
                status, note = "duplicate", f"same number as {seen[phone]}"
            elif phone:
                seen[phone] = name or f"row {row_no}"
            contacts.append({"row": row_no, "name": name, "phone": phone, "raw": raw, "status": status, "note": note})
    summary: dict[str, int] = {}
    for c in contacts:
        summary[c["status"]] = summary.get(c["status"], 0) + 1
    return {"columns": {k: v for k, v in cols.items() if v is not None} if has_header else {}, "contacts": contacts, "summary": summary}
