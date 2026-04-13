from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from app.config import settings

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(settings.MONGO_URI)
    return _client


def get_db() -> AsyncIOMotorDatabase:
    global _db
    if _db is None:
        _db = get_client()[settings.MONGO_DB]
    return _db


async def init_indexes() -> None:
    db = get_db()
    await db.users.create_index("email", unique=True)
    await db.leads.create_index([("user_id", 1), ("phone", 1)], unique=False)
    await db.leads.create_index([("user_id", 1), ("created_at", -1)])
    await db.leads.create_index("phone")
    await db.messages.create_index([("lead_id", 1), ("created_at", 1)])
    await db.messages.create_index([("user_id", 1), ("created_at", -1)])
    await db.messages.create_index("twilio_sid")
    await db.agents.create_index([("user_id", 1), ("created_at", -1)])
    await db.campaigns.create_index([("user_id", 1), ("created_at", -1)])
    await db.blast_campaigns.create_index([("user_id", 1), ("created_at", -1)])
    await db.blast_recipients.create_index([("blast_id", 1)])
    await db.blacklist.create_index([("user_id", 1), ("phone", 1)], unique=True)
    await db.webhook_events.create_index("twilio_sid", unique=True, sparse=True)
