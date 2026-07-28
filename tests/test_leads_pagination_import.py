"""E3–E6 leads pagination, search, import/export, bulk actions."""
from __future__ import annotations

import io
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException, UploadFile

from app.services.lead_query import (
    build_lead_filter,
    csv_safe_cell,
    pagination_params,
    sanitize_search,
    sort_spec,
)
from app.services.lead_import import import_leads_csv, _consent_fields_from_row
from app.services.lead_bulk import run_bulk_action, ALLOWED_BULK_ACTIONS


def test_pagination_defaults_and_max():
    page, ps, skip = pagination_params(1, None)
    assert page == 1
    assert ps == 25
    assert skip == 0
    page, ps, skip = pagination_params(3, 50)
    assert skip == 100
    with pytest.raises(HTTPException):
        pagination_params(0, 25)
    with pytest.raises(HTTPException):
        pagination_params(1, 101)


def test_sort_allowlist():
    assert sort_spec("created_at", "asc")[0] == ("created_at", 1)
    assert sort_spec(None, None)[1][0] == "_id"
    with pytest.raises(HTTPException):
        sort_spec("$where", "asc")
    with pytest.raises(HTTPException):
        sort_spec("created_at", "sideways")


def test_sanitize_search_rejects_unsafe():
    assert sanitize_search("  jane  ") == "jane"
    with pytest.raises(HTTPException):
        sanitize_search("$ne")
    with pytest.raises(HTTPException):
        sanitize_search("{" + "a" * 5)


def test_build_filter_tenant_and_score():
    q = build_lead_filter("u1", score="hot", blacklist_status="true")
    assert q["user_id"] == "u1"
    assert q["score"] == "hot"
    assert q["blacklisted"] is True


def test_build_filter_rejects_bad_score():
    with pytest.raises(HTTPException):
        build_lead_filter("u1", score="boiling")


def test_csv_formula_injection():
    assert csv_safe_cell("=1+1").startswith("'")
    assert csv_safe_cell("+cmd").startswith("'")
    assert csv_safe_cell("normal") == "normal"


def test_consent_import_defaults_unknown():
    fields, err = _consent_fields_from_row({})
    assert err is None
    assert fields["whatsapp_consent_status"] == "unknown"


def test_consent_import_opted_in_requires_evidence():
    fields, err = _consent_fields_from_row({"consent_status": "opted_in"})
    assert fields == {}
    assert err is not None
    fields, err = _consent_fields_from_row(
        {
            "consent_status": "opted_in",
            "consent_source": "website_form",
            "consent_at": "2026-01-01T00:00:00Z",
        }
    )
    assert err is None
    assert fields["whatsapp_consent_status"] == "opted_in"


@pytest.mark.asyncio
async def test_import_creates_and_skips_invalid_phone():
    db = MagicMock()
    db.leads.find = MagicMock(return_value=MagicMock(__aiter__=lambda self: self))

    async def empty_aiter():
        if False:
            yield None

    db.leads.find = MagicMock(return_value=empty_aiter())
    db.leads.insert_one = AsyncMock()
    db.leads.update_one = AsyncMock()

    csv_body = b"name,phone\nAlice,+447700900111\nBad,notaphone\n"
    upload = UploadFile(filename="leads.csv", file=io.BytesIO(csv_body))

    # Fix empty async iterator for find
    class Empty:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    db.leads.find = MagicMock(return_value=Empty())

    summary = await import_leads_csv(
        db, user_id="u1", upload=upload, duplicate_policy="skip"
    )
    assert summary["created"] == 1
    assert summary["failed"] >= 1
    assert db.leads.insert_one.await_count == 1


@pytest.mark.asyncio
async def test_import_file_size_limit(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "LEAD_IMPORT_MAX_FILE_MB", 0)  # 0 * 1024*1024 = 0 → max(1,0)=1 MB still
    monkeypatch.setattr(settings, "LEAD_IMPORT_MAX_FILE_MB", 1)
    # Create > 1MB by mocking read
    upload = MagicMock()
    upload.filename = "big.csv"
    upload.content_type = "text/csv"
    upload.read = AsyncMock(return_value=b"x" * (2 * 1024 * 1024))
    with pytest.raises(HTTPException) as exc:
        await import_leads_csv(MagicMock(), user_id="u1", upload=upload)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_bulk_rejects_opt_in_and_max():
    with pytest.raises(HTTPException):
        await run_bulk_action(
            MagicMock(),
            user_id="u1",
            lead_ids=["a"],
            action="opt_in",
        )
    assert "opt_in" not in ALLOWED_BULK_ACTIONS


@pytest.mark.asyncio
async def test_bulk_invalid_ids_and_cross_tenant_skip():
    db = MagicMock()
    db.leads.find_one = AsyncMock(return_value=None)
    db.leads.update_one = AsyncMock()
    with patch("app.services.lead_bulk.ws_manager.push", new=AsyncMock()):
        with patch("app.services.lead_bulk.audit"):
            res = await run_bulk_action(
                db,
                user_id="tenantA",
                lead_ids=[str(ObjectId()), "not-an-id"],
                action="pause_ai",
            )
    assert res["skipped"] >= 1
    assert res["failed"] >= 1
    assert res["affected"] == 0


@pytest.mark.asyncio
async def test_list_leads_paginated_shape():
    from app.routes import leads as leads_route

    user_id = ObjectId()
    db = MagicMock()
    db.leads.count_documents = AsyncMock(return_value=2)

    class Cur:
        def sort(self, *a, **k):
            return self

        def skip(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "_n", 0) >= 1:
                raise StopAsyncIteration
            self._n = getattr(self, "_n", 0) + 1
            return {
                "_id": ObjectId(),
                "user_id": str(user_id),
                "name": "A",
                "phone": "+447700900001",
                "score": "cold",
                "tags": [],
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }

    db.leads.find = MagicMock(return_value=Cur())
    with patch.object(leads_route, "get_db", return_value=db):
        result = await leads_route.list_leads(
            user={"_id": user_id},
            page=1,
            page_size=25,
        )
    assert "items" in result
    assert result["page"] == 1
    assert result["page_size"] == 25
    assert result["total"] == 2
    assert isinstance(result["items"], list)


@pytest.mark.asyncio
async def test_bulk_opt_out_updates_consent():
    from app.services import lead_bulk

    oid = ObjectId()
    db = MagicMock()
    db.leads.find_one = AsyncMock(
        side_effect=[
            {"_id": oid, "phone": "+447700900099", "user_id": "u1"},
            {
                "_id": oid,
                "phone": "+447700900099",
                "user_id": "u1",
                "whatsapp_consent_status": "opted_out",
            },
        ]
    )
    with (
        patch.object(lead_bulk, "apply_consent_change", new=AsyncMock(return_value={})),
        patch("app.services.lead_scoring.recalculate_lead_score", new=AsyncMock()),
        patch.object(lead_bulk.ws_manager, "push", new=AsyncMock()),
        patch.object(lead_bulk, "audit"),
    ):
        res = await run_bulk_action(
            db,
            user_id="u1",
            lead_ids=[str(oid)],
            action="opt_out",
        )
    assert res["affected"] == 1
