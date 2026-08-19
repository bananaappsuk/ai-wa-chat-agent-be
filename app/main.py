import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.mongo import close_client as close_mongo, init_indexes
from app.middleware.errors import register_exception_handlers
from app.middleware.security import RequestIdMiddleware, SecurityHeadersMiddleware
from app.observability.logging_setup import configure_logging
from app.observability.metrics import MetricsMiddleware
from app.observability.sentry import capture_exception, init_sentry
from app.routes import (
    auth,
    leads,
    messages,
    agents,
    campaigns,
    profile,
    admin,
    webhook,
    meta_webhook,
    meta_poc,
    blacklist,
    dashboard,
    templates,
    media,
    settings as settings_route,
    ws as ws_route,
    activity,
    notifications,
    conversations,
    analytics,
    billing,
)
from app.observability import metrics as metrics_route
from app.workers.queue import close_redis, get_redis

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    init_sentry(service="api")
    settings.validate_for_startup()
    logger.info(
        "API starting env=%s inline_scheduler=%s",
        settings.APP_ENV,
        settings.run_inline_scheduler,
    )
    try:
        await init_indexes()
    except Exception as exc:
        logger.exception("Index initialisation failed")
        capture_exception(exc, phase="init_indexes")
        # Stay up in non-prod so /ready can report unhealthy; fail hard in production-like
        if settings.is_production_like:
            raise

    task = asyncio.create_task(ws_route.redis_pubsub_loop())
    sched = None
    if settings.run_inline_scheduler:
        sched = asyncio.create_task(_scheduled_campaigns_loop())
        logger.info("Inline campaign scheduler enabled (dev/test default)")
    else:
        logger.info("Inline campaign scheduler disabled — run python scheduler.py separately")

    try:
        yield
    finally:
        logger.info("API shutting down")
        tasks = [task]
        if sched is not None:
            tasks.append(sched)
        for t in tasks:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        close_redis()
        close_mongo()
        logger.info("API shutdown complete")


async def _scheduled_campaigns_loop() -> None:
    """Enqueue due scheduled campaigns about once a minute (single-process only)."""
    from app.workers.queue import enqueue
    from app.workers import campaign_tasks

    while True:
        try:
            await asyncio.sleep(max(15, int(settings.SCHEDULER_INTERVAL_SECONDS)))
            enqueue(campaign_tasks.process_due_scheduled_campaigns)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Inline scheduler tick failed")
            capture_exception(exc, phase="inline_scheduler")
            await asyncio.sleep(60)


app = FastAPI(title="AI WhatsApp Chat Agent API", lifespan=lifespan)

# Middleware order: last added runs first on request.
app.add_middleware(MetricsMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Accept", "X-Metrics-Token", "Idempotency-Key"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)

register_exception_handlers(app)


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "status": "up"}


@app.get("/ready")
async def ready():
    from fastapi.responses import JSONResponse
    from app.observability.metrics import set_dependency_ready

    checks: dict = {
        "mongo": False,
        "redis": False,
        "twilio_configured": bool(
            settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN
        ),
        "openai_configured": bool(settings.OPENAI_API_KEY)
        if settings.AI_FEATURES_ENABLED
        else None,
        "public_base_url_configured": bool((settings.PUBLIC_BASE_URL or "").strip()),
    }
    ok = True
    try:
        from app.db.mongo import get_client

        await get_client().admin.command("ping")
        checks["mongo"] = True
    except Exception:
        ok = False
    try:
        get_redis().ping()
        checks["redis"] = True
    except Exception:
        ok = False

    set_dependency_ready(mongo=checks["mongo"], redis=checks["redis"])
    body = {"ok": ok, "checks": checks}
    return JSONResponse(status_code=200 if ok else 503, content=body)


api_prefix = "/api"
app.include_router(auth.router, prefix=api_prefix)
app.include_router(profile.router, prefix=api_prefix)
app.include_router(settings_route.router, prefix=api_prefix)
app.include_router(leads.router, prefix=api_prefix)
app.include_router(messages.router, prefix=api_prefix)
app.include_router(agents.router, prefix=api_prefix)
app.include_router(campaigns.router, prefix=api_prefix)
app.include_router(blacklist.router, prefix=api_prefix)
app.include_router(dashboard.router, prefix=api_prefix)
app.include_router(admin.router, prefix=api_prefix)
app.include_router(webhook.router, prefix=api_prefix)
app.include_router(meta_webhook.router, prefix=api_prefix)
app.include_router(meta_poc.router, prefix=api_prefix)
app.include_router(templates.router, prefix=api_prefix)
app.include_router(media.router, prefix=api_prefix)
app.include_router(activity.router, prefix=api_prefix)
app.include_router(notifications.router, prefix=api_prefix)
app.include_router(conversations.router, prefix=api_prefix)
app.include_router(analytics.router, prefix=api_prefix)
app.include_router(billing.router, prefix=api_prefix)
app.include_router(ws_route.router)
app.include_router(metrics_route.router)

