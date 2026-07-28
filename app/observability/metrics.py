"""Prometheus-compatible metrics (low-cardinality labels only)."""
from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

try:
    from prometheus_client import (
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
        REGISTRY,
    )

    _HAS_PROM = True
except ImportError:  # pragma: no cover
    _HAS_PROM = False

router = APIRouter(tags=["metrics"])

if _HAS_PROM:
    HTTP_REQUESTS = Counter(
        "http_requests_total",
        "HTTP requests",
        ["method", "status_class", "route_group"],
    )
    HTTP_DURATION = Histogram(
        "http_request_duration_seconds",
        "HTTP request duration",
        ["method", "route_group"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    )
    HTTP_ERRORS = Counter("http_errors_total", "HTTP 5xx responses", ["route_group"])
    WS_CONNECTIONS = Gauge("websocket_connections_active", "Active WebSocket connections")
    WEBHOOK_INBOUND = Counter("twilio_webhook_inbound_total", "Inbound WhatsApp webhooks")
    WEBHOOK_SIG_FAIL = Counter("twilio_webhook_signature_failures_total", "Invalid Twilio signatures")
    OUTBOUND_ATTEMPTS = Counter("outbound_send_attempts_total", "Outbound send attempts")
    OUTBOUND_OK = Counter("outbound_send_success_total", "Outbound sends succeeded")
    OUTBOUND_FAIL = Counter("outbound_send_failed_total", "Outbound sends failed")
    CAMPAIGN_SENDS = Counter("campaign_recipient_sends_total", "Campaign recipient send attempts")
    AI_ATTEMPTS = Counter("ai_jobs_attempted_total", "AI reply jobs attempted")
    AI_FAIL = Counter("ai_jobs_failed_total", "AI reply jobs failed")
    AI_OP = Counter("ai_operations_total", "AI operations by type and status", ["operation", "status"])
    AI_TOKENS = Counter("ai_tokens_total", "AI tokens by operation", ["operation"])
    AI_LATENCY = Histogram(
        "ai_operation_latency_seconds",
        "AI operation latency",
        ["operation"],
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    )
    AI_QUOTA_BLOCKS = Counter("ai_quota_blocks_total", "AI quota blocks")
    AI_MOD_BLOCKS = Counter("ai_moderation_blocks_total", "AI moderation blocks")
    MONGO_READY = Gauge("mongo_ready", "1 if Mongo ping ok")
    REDIS_READY = Gauge("redis_ready", "1 if Redis ping ok")
    POLICY_BLOCKED = Counter(
        "outbound_policy_blocked_total",
        "Outbound sends blocked by policy",
        ["reason"],
    )
    CONSENT_OPT_OUT = Counter("whatsapp_opt_outs_total", "WhatsApp opt-outs")
    CONSENT_OPT_IN = Counter("whatsapp_opt_ins_total", "WhatsApp opt-ins")
    DUPLICATE_PREVENTED = Counter("duplicate_sends_prevented_total", "Duplicate sends prevented")
    PROVIDER_RATE_LIMIT = Counter("twilio_rate_limit_events_total", "Twilio rate-limit events")
    RETRY_SCHEDULED = Counter("outbound_retries_scheduled_total", "Outbound retries scheduled")
    RETRY_EXHAUSTED = Counter("outbound_retries_exhausted_total", "Outbound retries exhausted")
    RECONCILE_REPAIRS = Counter("message_reconciliation_repairs_total", "Reconciliation repairs")
    PROVIDER_FAIL = Counter(
        "provider_failures_total",
        "Provider failures by category",
        ["category"],
    )
    QUEUE_DEPTH = Gauge("rq_queue_depth", "RQ queue depth", ["queue"])
    SEND_LATENCY = Histogram(
        "outbound_send_latency_seconds",
        "Outbound send latency",
        buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
    )
    SELF_SENDER_LOOP = Counter(
        "whatsapp_self_sender_loop_total", "Inbound webhooks ignored because From matched our own sender"
    )
else:  # pragma: no cover
    HTTP_REQUESTS = HTTP_DURATION = HTTP_ERRORS = WS_CONNECTIONS = None  # type: ignore
    WEBHOOK_INBOUND = WEBHOOK_SIG_FAIL = OUTBOUND_ATTEMPTS = OUTBOUND_OK = OUTBOUND_FAIL = None  # type: ignore
    CAMPAIGN_SENDS = AI_ATTEMPTS = AI_FAIL = MONGO_READY = REDIS_READY = None  # type: ignore
    AI_OP = AI_TOKENS = AI_LATENCY = AI_QUOTA_BLOCKS = AI_MOD_BLOCKS = None  # type: ignore
    POLICY_BLOCKED = CONSENT_OPT_OUT = CONSENT_OPT_IN = DUPLICATE_PREVENTED = None  # type: ignore
    PROVIDER_RATE_LIMIT = RETRY_SCHEDULED = RETRY_EXHAUSTED = RECONCILE_REPAIRS = None  # type: ignore
    PROVIDER_FAIL = QUEUE_DEPTH = SEND_LATENCY = SELF_SENDER_LOOP = None  # type: ignore


def _route_group(path: str) -> str:
    if path.startswith("/api/webhook"):
        return "webhook"
    if path.startswith("/api/auth"):
        return "auth"
    if path.startswith("/api/media"):
        return "media"
    if path.startswith("/api/campaigns") or path.startswith("/api/blasts"):
        return "campaigns"
    if path.startswith("/ws"):
        return "websocket"
    if path in ("/health", "/ready", "/metrics"):
        return "ops"
    if path.startswith("/api/"):
        return "api"
    return "other"


def _status_class(code: int) -> str:
    return f"{code // 100}xx"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.METRICS_ENABLED or not _HAS_PROM:
            return await call_next(request)
        if request.url.path == "/metrics":
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start
        group = _route_group(request.url.path)
        method = request.method
        code = response.status_code
        try:
            HTTP_REQUESTS.labels(method, _status_class(code), group).inc()
            HTTP_DURATION.labels(method, group).observe(duration)
            if code >= 500:
                HTTP_ERRORS.labels(group).inc()
        except Exception:
            pass
        return response


def inc_webhook_inbound() -> None:
    if WEBHOOK_INBOUND:
        WEBHOOK_INBOUND.inc()


def inc_webhook_sig_fail() -> None:
    if WEBHOOK_SIG_FAIL:
        WEBHOOK_SIG_FAIL.inc()


def inc_outbound(*, ok: bool) -> None:
    if OUTBOUND_ATTEMPTS:
        OUTBOUND_ATTEMPTS.inc()
    if ok and OUTBOUND_OK:
        OUTBOUND_OK.inc()
    elif not ok and OUTBOUND_FAIL:
        OUTBOUND_FAIL.inc()


def inc_campaign_send() -> None:
    if CAMPAIGN_SENDS:
        CAMPAIGN_SENDS.inc()


def inc_ai(*, ok: bool) -> None:
    if AI_ATTEMPTS:
        AI_ATTEMPTS.inc()
    if not ok and AI_FAIL:
        AI_FAIL.inc()


def observe_ai_call(*, operation: str, ok: bool, tokens: int = 0, latency_ms: int = 0) -> None:
    """Low-cardinality AI operation metrics."""
    op = (operation or "unknown")[:32]
    if not _HAS_PROM:
        return
    try:
        AI_OP.labels(op, "ok" if ok else "fail").inc()
        if tokens and AI_TOKENS:
            AI_TOKENS.labels(op).inc(max(0, int(tokens)))
        if latency_ms and AI_LATENCY:
            AI_LATENCY.labels(op).observe(max(0, int(latency_ms)) / 1000.0)
    except Exception:
        pass
    inc_ai(ok=ok)


def set_ws_connections(n: int) -> None:
    if WS_CONNECTIONS:
        WS_CONNECTIONS.set(max(0, n))


def set_dependency_ready(*, mongo: Optional[bool] = None, redis: Optional[bool] = None) -> None:
    if mongo is not None and MONGO_READY:
        MONGO_READY.set(1 if mongo else 0)
    if redis is not None and REDIS_READY:
        REDIS_READY.set(1 if redis else 0)


_SAFE_REASONS = frozenset(
    {
        "ok",
        "consent_blocked",
        "consent_required",
        "window_closed",
        "configuration_error",
        "invalid_recipient",
        "template_error",
        "media_error",
        "authentication_error",
        "provider_rate_limited",
        "retryable",
        "non_retryable",
        "other",
    }
)


def _safe_label(value: str, allowed: frozenset[str]) -> str:
    v = (value or "other").strip().lower()[:40]
    return v if v in allowed else "other"


def inc_policy_blocked(reason: str) -> None:
    if POLICY_BLOCKED:
        POLICY_BLOCKED.labels(_safe_label(reason, _SAFE_REASONS)).inc()


def inc_consent_opt_out() -> None:
    if CONSENT_OPT_OUT:
        CONSENT_OPT_OUT.inc()


def inc_consent_opt_in() -> None:
    if CONSENT_OPT_IN:
        CONSENT_OPT_IN.inc()


def inc_duplicate_prevented() -> None:
    if DUPLICATE_PREVENTED:
        DUPLICATE_PREVENTED.inc()


def inc_retry_scheduled() -> None:
    if RETRY_SCHEDULED:
        RETRY_SCHEDULED.inc()


def inc_retry_exhausted() -> None:
    if RETRY_EXHAUSTED:
        RETRY_EXHAUSTED.inc()


def inc_reconciliation_repairs(n: int = 1) -> None:
    if RECONCILE_REPAIRS and n:
        RECONCILE_REPAIRS.inc(n)


def inc_provider_failure(category: str) -> None:
    if category == "provider_rate_limited" and PROVIDER_RATE_LIMIT:
        PROVIDER_RATE_LIMIT.inc()
    if PROVIDER_FAIL:
        PROVIDER_FAIL.labels(_safe_label(category, _SAFE_REASONS)).inc()


def inc_self_sender_loop() -> None:
    if SELF_SENDER_LOOP:
        SELF_SENDER_LOOP.inc()


def set_queue_depth(queue: str, depth: int) -> None:
    if QUEUE_DEPTH:
        q = (queue or "default")[:40]
        QUEUE_DEPTH.labels(q).set(max(0, int(depth)))


@router.get("/metrics")
async def metrics(
    authorization: Optional[str] = Header(default=None),
    x_metrics_token: Optional[str] = Header(default=None),
):
    if not settings.METRICS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")
    token = (settings.METRICS_TOKEN or "").strip()
    # Never expose /metrics without a configured shared secret.
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    provided = (x_metrics_token or "").strip()
    if not provided and authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if provided != token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not _HAS_PROM:
        return Response(content="# prometheus_client not installed\n", media_type="text/plain")
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
