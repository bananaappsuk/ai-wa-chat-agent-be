"""Central exception handlers — never leak stack traces or secrets to clients."""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.errors import DuplicateKeyError

from app.middleware.security import get_request_id
from app.security.audit import sanitize_error_message

logger = logging.getLogger("app.errors")


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, str):
            detail = sanitize_error_message(detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": detail, "request_id": get_request_id()},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # Keep field errors but strip any unexpected internals
        errors = []
        for err in exc.errors()[:40]:
            errors.append(
                {
                    "loc": err.get("loc"),
                    "msg": sanitize_error_message(str(err.get("msg") or "invalid"), max_len=120),
                    "type": err.get("type"),
                }
            )
        return JSONResponse(
            status_code=422,
            content={"detail": errors, "request_id": get_request_id()},
        )

    @app.exception_handler(DuplicateKeyError)
    async def duplicate_key_handler(request: Request, exc: DuplicateKeyError):
        return JSONResponse(
            status_code=409,
            content={"detail": "Resource already exists", "request_id": get_request_id()},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        rid = get_request_id()
        logger.exception(
            "Unhandled error request_id=%s path=%s exc=%s",
            rid,
            request.url.path,
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error", "request_id": rid},
        )
