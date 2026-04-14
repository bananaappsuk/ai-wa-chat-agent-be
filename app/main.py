import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.db.mongo import close_client as close_mongo, init_indexes
from app.routes import auth, leads, messages, agents, campaigns, profile, admin, webhook, blacklist, dashboard, ws as ws_route
from app.workers.queue import close_redis


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_indexes()
    task = asyncio.create_task(ws_route.redis_pubsub_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
        close_redis()
        close_mongo()


app = FastAPI(title="AI WhatsApp Chat Agent API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


api_prefix = "/api"
app.include_router(auth.router, prefix=api_prefix)
app.include_router(profile.router, prefix=api_prefix)
app.include_router(leads.router, prefix=api_prefix)
app.include_router(messages.router, prefix=api_prefix)
app.include_router(agents.router, prefix=api_prefix)
app.include_router(campaigns.router, prefix=api_prefix)
app.include_router(blacklist.router, prefix=api_prefix)
app.include_router(dashboard.router, prefix=api_prefix)
app.include_router(admin.router, prefix=api_prefix)
app.include_router(webhook.router, prefix=api_prefix)
app.include_router(ws_route.router)
