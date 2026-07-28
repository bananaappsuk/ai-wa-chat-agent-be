"""CSV lead import (tenant-safe, consent-aware)."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import HTTPException, UploadFile

from app.config import settings
from app.models.common import utcnow
from app.security.validation import reject_mongo_operators
from app.services.lead_query import is_valid_email
from app.services.lead_service import _norm_phone
from app.services.whatsapp_consent import CONSENT_DEFAULTS

DuplicatePolicy = Literal["skip", "update", "fail"]

REQUIRED_HEADERS = {"phone"}
OPTIONAL_HEADERS = {
    "name",
    "email",
    "company",
    "source",
    "tags",
    "consent_status",
    "consent_source",
    "consent_at",
}
ALLOWED_HEADERS = REQUIRED_HEADERS | OPTIONAL_HEADERS
UPDATE_FIELDS = frozenset({"name", "email", "company", "source", "tags"})


def _parse_dt(raw: str) -> Optional[datetime]:
    text = (raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _normalize_header(h: str) -> str:
    return (h or "").strip().lstrip("\ufeff").lower().replace(" ", "_")


def _parse_tags(raw: str) -> list[str]:
    parts = [p.strip()[:40] for p in (raw or "").split(",")]
    return [p for p in parts if p][:50]


def _validate_phone(raw: str) -> Optional[str]:
    p = _norm_phone((raw or "").strip())
    if not p or not p.startswith("+"):
        return None
    digits = p[1:]
    if not digits.isdigit() or not (8 <= len(digits) <= 15):
        return None
    return p


def _consent_fields_from_row(row: dict[str, str]) -> tuple[dict[str, Any], Optional[str]]:
    """Return consent fields + optional error message. Never auto opt-in without evidence."""
    status = (row.get("consent_status") or "").strip().lower() or "unknown"
    source = (row.get("consent_source") or "").strip()[:40] or None
    consent_at = _parse_dt(row.get("consent_at") or "")

    if status not in ("unknown", "pending", "opted_in", "opted_out"):
        return {}, f"Invalid consent_status '{status}'"

    if status == "opted_in":
        if not source or not consent_at:
            return {}, "opted_in requires consent_source and consent_at"
        return {
            "whatsapp_consent_status": "opted_in",
            "whatsapp_consent_source": source,
            "whatsapp_consent_at": consent_at,
            "whatsapp_consent_updated_at": utcnow(),
            "whatsapp_opted_out_at": None,
            "whatsapp_opt_out_reason": None,
            "blacklisted": False,
        }, None

    if status == "opted_out":
        return {
            "whatsapp_consent_status": "opted_out",
            "whatsapp_consent_source": source or "import",
            "whatsapp_consent_updated_at": utcnow(),
            "whatsapp_opted_out_at": consent_at or utcnow(),
            "whatsapp_opt_out_reason": "import",
            "blacklisted": True,
            "ai_paused": True,
        }, None

    # unknown / pending — never treat as marketing opt-in
    fields = {**CONSENT_DEFAULTS, "whatsapp_consent_status": status}
    if source:
        fields["whatsapp_consent_source"] = source
    if consent_at and status == "pending":
        fields["whatsapp_consent_at"] = consent_at
    return fields, None


async def read_csv_rows(upload: UploadFile) -> list[dict[str, str]]:
    max_bytes = max(1, int(settings.LEAD_IMPORT_MAX_FILE_MB)) * 1024 * 1024
    raw = await upload.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"File exceeds {settings.LEAD_IMPORT_MAX_FILE_MB}MB limit",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="Empty CSV file")

    filename = (upload.filename or "").lower()
    if filename and not filename.endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are supported")
    content_type = (upload.content_type or "").lower()
    if content_type and content_type not in (
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "application/octet-stream",
        "text/plain",
    ):
        raise HTTPException(status_code=400, detail="Invalid CSV content type")

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded") from exc

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise HTTPException(status_code=400, detail="CSV missing header row")

    headers = {_normalize_header(h) for h in reader.fieldnames if h}
    if not REQUIRED_HEADERS.issubset(headers):
        raise HTTPException(status_code=400, detail="CSV must include a phone column")
    unknown = headers - ALLOWED_HEADERS
    # Allow unknown headers but ignore them (safer than hard fail for extra columns)
    _ = unknown

    max_rows = max(1, int(settings.LEAD_IMPORT_MAX_ROWS))
    rows: list[dict[str, str]] = []
    for i, row in enumerate(reader, start=2):  # row 1 = header
        if i - 1 > max_rows:
            raise HTTPException(
                status_code=400,
                detail=f"CSV exceeds maximum of {max_rows} data rows",
            )
        normalized = {_normalize_header(k): (v or "").strip() for k, v in row.items() if k}
        rows.append(normalized)
    return rows


async def import_leads_csv(
    db,
    *,
    user_id: str,
    upload: UploadFile,
    duplicate_policy: DuplicatePolicy = "skip",
) -> dict[str, Any]:
    if duplicate_policy not in ("skip", "update", "fail"):
        raise HTTPException(status_code=400, detail="duplicate_policy must be skip, update, or fail")

    rows = await read_csv_rows(upload)
    max_errors = max(1, int(settings.LEAD_IMPORT_MAX_ERRORS_RETURNED))
    batch_size = max(1, int(settings.LEAD_IMPORT_BATCH_SIZE))

    created = updated = skipped = failed = 0
    errors: list[dict[str, Any]] = []
    seen_phones: set[str] = set()
    now = utcnow()

    def add_error(row_num: int, field: str, message: str) -> None:
        nonlocal failed
        failed += 1
        if len(errors) < max_errors:
            errors.append({"row": row_num, "field": field, "message": message})

    # Process in batches for DB lookups
    for start in range(0, len(rows), batch_size):
        chunk = rows[start : start + batch_size]
        phones = []
        for offset, row in enumerate(chunk):
            row_num = start + offset + 2
            phone = _validate_phone(row.get("phone") or "")
            if phone:
                phones.append(phone)

        existing_map: dict[str, dict] = {}
        if phones:
            async for doc in db.leads.find(
                {"user_id": user_id, "phone": {"$in": list(set(phones))}}
            ):
                existing_map[doc["phone"]] = doc

        for offset, row in enumerate(chunk):
            row_num = start + offset + 2
            reject_mongo_operators(row, path=f"row[{row_num}]")

            phone = _validate_phone(row.get("phone") or "")
            if not phone:
                add_error(row_num, "phone", "Invalid phone number")
                continue

            if phone in seen_phones:
                skipped += 1
                if len(errors) < max_errors:
                    errors.append(
                        {
                            "row": row_num,
                            "field": "phone",
                            "message": "Duplicate phone within file",
                        }
                    )
                continue
            seen_phones.add(phone)

            email = (row.get("email") or "").strip()
            if email and not is_valid_email(email):
                add_error(row_num, "email", "Invalid email")
                continue

            consent_fields, consent_err = _consent_fields_from_row(row)
            if consent_err:
                add_error(row_num, "consent_status", consent_err)
                continue

            name = (row.get("name") or "").strip()[:100] or phone
            tags = _parse_tags(row.get("tags") or "")
            source = (row.get("source") or "").strip()[:100] or "import"
            company = (row.get("company") or "").strip()[:100] or None

            existing = existing_map.get(phone)
            if existing:
                if duplicate_policy == "skip":
                    skipped += 1
                    continue
                if duplicate_policy == "fail":
                    add_error(row_num, "phone", "Lead already exists")
                    continue
                # update — never overwrite opted_out with unknown; never lift blacklist via import
                update: dict[str, Any] = {
                    "name": name,
                    "updated_at": now,
                }
                if email:
                    update["email"] = email
                if company is not None:
                    update["company"] = company
                if source:
                    update["source"] = source
                if tags:
                    update["tags"] = tags

                existing_status = (existing.get("whatsapp_consent_status") or "unknown").lower()
                new_status = consent_fields.get("whatsapp_consent_status", "unknown")
                if existing_status == "opted_out" and new_status != "opted_out":
                    # Do not convert opted_out → opted_in / unknown via import update
                    pass
                elif existing.get("blacklisted") and new_status == "opted_in":
                    pass
                elif new_status == "unknown" and existing_status in ("opted_in", "opted_out", "pending"):
                    # Do not overwrite existing consent with unknown
                    pass
                else:
                    update.update(consent_fields)

                await db.leads.update_one(
                    {"_id": existing["_id"], "user_id": user_id},
                    {"$set": update},
                )
                if any(k in update for k in ("tags", "source", "phone", "blacklisted")):
                    from app.services.lead_scoring import recalculate_lead_score

                    await recalculate_lead_score(user_id, str(existing["_id"]))
                updated += 1
                continue

            doc = {
                "user_id": user_id,
                "name": name,
                "phone": phone,
                "email": email or None,
                "company": company,
                "score": "cold",
                "lead_score": 0,
                "score_updated_at": now,
                "source": source,
                "tags": tags,
                "blacklisted": bool(consent_fields.get("blacklisted")),
                "ai_paused": bool(consent_fields.get("ai_paused", False)),
                "needs_human": False,
                "takeover_by": None,
                "takeover_at": None,
                "last_inbound_at": None,
                "whatsapp_window_expires_at": None,
                **{**CONSENT_DEFAULTS, **consent_fields},
                "created_at": now,
                "updated_at": now,
            }
            await db.leads.insert_one(doc)
            created += 1

    return {
        "total_rows": len(rows),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
        "errors_truncated": len(errors) < failed,
    }
