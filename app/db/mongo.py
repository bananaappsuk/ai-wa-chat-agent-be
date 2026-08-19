from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
import logging

from app.config import settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_client() -> AsyncIOMotorClient:
    global _client
    if _client is None:
        _client = AsyncIOMotorClient(
            settings.MONGO_URI,
            serverSelectionTimeoutMS=max(1000, int(settings.MONGO_SERVER_SELECTION_TIMEOUT_MS)),
            connectTimeoutMS=max(1000, int(settings.MONGO_CONNECT_TIMEOUT_MS)),
            maxPoolSize=max(1, int(settings.MONGO_MAX_POOL_SIZE)),
        )
        logger.info(
            "MongoDB client created db=%s pool=%s",
            settings.MONGO_DB,
            settings.MONGO_MAX_POOL_SIZE,
        )
    return _client


def get_db() -> AsyncIOMotorDatabase:
    global _db
    if _db is None:
        _db = get_client()[settings.MONGO_DB]
    return _db


def close_client() -> None:
    global _client, _db
    if _client is not None:
        try:
            _client.close()
            logger.info("MongoDB client closed")
        except Exception:
            pass
    _client = None
    _db = None


async def _ensure_leads_phone_index(db: AsyncIOMotorDatabase) -> None:
    """
    D6: enforce (user_id, phone) uniqueness once it is safe to do so.

    MongoDB refuses to create a unique index while duplicate key groups exist,
    and creating two indexes with the same key spec but different `unique`
    options raises IndexOptionsConflict. So on every startup we:
      1. Aggregate for duplicate (user_id, phone) groups.
      2. If any exist, keep/create a non-unique index and log a warning
         (run `python -m scripts.report_duplicate_leads` to inspect them).
      3. Otherwise create/keep the index as unique.
    """
    key = [("user_id", 1), ("phone", 1)]
    dup_pipeline = [
        {"$match": {"phone": {"$type": "string"}}},
        {"$group": {"_id": {"user_id": "$user_id", "phone": "$phone"}, "n": {"$sum": 1}}},
        {"$match": {"n": {"$gt": 1}}},
        {"$limit": 1},
    ]
    has_dupes = False
    async for _ in db.leads.aggregate(dup_pipeline):
        has_dupes = True
        break

    existing = await db.leads.index_information()
    current_name: str | None = None
    current_unique: bool | None = None
    for name, info in existing.items():
        if info.get("key") == key:
            current_name = name
            current_unique = bool(info.get("unique"))
            break

    want_unique = not has_dupes
    if current_name is not None and current_unique == want_unique:
        return

    if current_name is not None and current_unique != want_unique:
        # Can't have two indexes on the same keys with different options.
        await db.leads.drop_index(current_name)

    if has_dupes:
        logger.warning(
            "Duplicate (user_id, phone) lead groups detected — keeping the "
            "index non-unique. Run `python -m scripts.report_duplicate_leads` "
            "to review duplicates before they can be merged and uniqueness enabled."
        )
        await db.leads.create_index(key, unique=False)
    else:
        await db.leads.create_index(key, unique=True)


async def init_indexes() -> None:
    """Idempotent index creation — safe to run on every startup."""
    db = get_db()
    await db.users.create_index("email", unique=True)
    # Unique only when a string number is set — many users may have no WhatsApp number.
    await db.users.create_index(
        "twilio_whatsapp_to",
        unique=True,
        partialFilterExpression={"twilio_whatsapp_to": {"$type": "string"}},
    )
    # Meta Cloud API inbound routing — unique when a Phone Number ID is stored.
    await db.users.create_index(
        "meta_phone_number_id",
        unique=True,
        partialFilterExpression={"meta_phone_number_id": {"$type": "string"}},
    )
    await _ensure_leads_phone_index(db)
    await db.leads.create_index([("user_id", 1), ("created_at", -1)])
    await db.leads.create_index([("user_id", 1), ("updated_at", -1)])
    await db.leads.create_index([("user_id", 1), ("score", 1)])
    await db.leads.create_index([("user_id", 1), ("lead_score", -1)])
    await db.leads.create_index([("user_id", 1), ("whatsapp_consent_status", 1)])
    await db.leads.create_index([("user_id", 1), ("blacklisted", 1)])
    await db.leads.create_index([("user_id", 1), ("source", 1)])
    await db.leads.create_index([("user_id", 1), ("last_inbound_at", -1)])
    await db.leads.create_index("phone")
    await db.messages.create_index([("lead_id", 1), ("created_at", 1)])
    await db.messages.create_index([("user_id", 1), ("created_at", -1)])
    # Kept non-unique (historical SID duplicates possible). Unique+sparse requires a data cleanup migration.
    await db.messages.create_index("twilio_sid")
    await db.messages.create_index(
        [("provider", 1), ("provider_message_id", 1)],
        unique=True,
        partialFilterExpression={
            "provider": {"$type": "string"},
            "provider_message_id": {"$type": "string"},
        },
    )
    await db.agents.create_index([("user_id", 1), ("created_at", -1)])
    await db.campaigns.create_index([("user_id", 1), ("created_at", -1)])
    await db.campaigns.create_index([("status", 1), ("scheduled_at", 1)])
    await db.campaigns.create_index([("user_id", 1), ("content_mode", 1), ("created_at", -1)])
    await db.campaigns.create_index([("user_id", 1), ("agent_id", 1), ("created_at", -1)])
    await db.campaigns.create_index([("status", 1), ("ai_generation_status", 1)])
    await db.campaign_recipients.create_index([("campaign_id", 1), ("status", 1)])
    await db.campaign_recipients.create_index([("user_id", 1), ("phone", 1)])
    await db.campaign_recipients.create_index([("campaign_id", 1), ("phone", 1)], unique=True)
    await db.campaign_recipients.create_index("message_id", sparse=True)
    await db.campaign_recipients.create_index("twilio_sid", sparse=True)
    await db.campaign_recipients.create_index([("campaign_id", 1), ("ai_generation_status", 1)])
    await db.campaign_recipients.create_index([("campaign_id", 1), ("ai_approved", 1)])
    await db.campaign_recipients.create_index([("campaign_id", 1), ("content_source", 1)])
    await db.campaign_recipients.create_index(
        [("campaign_id", 1), ("ai_idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"ai_idempotency_key": {"$type": "string"}},
    )
    await db.campaign_ai_previews.create_index(
        [("campaign_id", 1), ("user_id", 1), ("created_at", -1)]
    )
    await db.blast_campaigns.create_index([("user_id", 1), ("created_at", -1)])
    await db.blast_recipients.create_index([("blast_id", 1)])
    await db.blast_recipients.create_index("twilio_sid", sparse=True)
    await db.blacklist.create_index([("user_id", 1), ("phone", 1)], unique=True)
    await db.webhook_events.create_index("twilio_sid", unique=True, sparse=True)
    await db.webhook_events.create_index(
        [("provider", 1), ("provider_message_id", 1)],
        unique=True,
        partialFilterExpression={
            "provider": {"$type": "string"},
            "provider_message_id": {"$type": "string"},
        },
    )
    # Idempotency SIDs only need short retention; TTL prevents unbounded growth (D7).
    await db.webhook_events.create_index("received_at", expireAfterSeconds=60 * 60 * 24 * 30)
    await db.templates.create_index([("user_id", 1), ("updated_at", -1)])
    await db.templates.create_index([("user_id", 1), ("name", 1)])
    await db.templates.create_index([("user_id", 1), ("content_sid", 1)])
    await db.consent_events.create_index([("user_id", 1), ("lead_id", 1), ("created_at", -1)])
    await db.messages.create_index(
        [("user_id", 1), ("idempotency_key", 1)],
        unique=True,
        partialFilterExpression={"idempotency_key": {"$type": "string"}},
    )
    await db.messages.create_index([("direction", 1), ("status", 1), ("created_at", 1)])

    # Users (E7–E9)
    await db.users.create_index([("role", 1), ("active", 1)])
    await db.users.create_index("active")
    await db.users.create_index([("created_at", -1)])

    # Password reset tokens
    await db.password_reset_tokens.create_index("token_hash", unique=True)
    await db.password_reset_tokens.create_index("user_id")
    await db.password_reset_tokens.create_index("expires_at", expireAfterSeconds=0)

    # Notifications
    await db.notifications.create_index([("user_id", 1), ("is_read", 1), ("created_at", -1)])
    await db.notifications.create_index([("tenant_id", 1), ("created_at", -1)])
    await db.notifications.create_index(
        [("user_id", 1), ("dedupe_key", 1)],
        unique=True,
        partialFilterExpression={"dedupe_key": {"$type": "string"}},
    )

    # Activity / audit events
    await db.activity_events.create_index([("tenant_id", 1), ("created_at", -1)])
    await db.activity_events.create_index([("resource_type", 1), ("resource_id", 1)])
    await db.activity_events.create_index("event_type")
    await db.activity_events.create_index("actor_id")

    # AI (C6–C17)
    await db.conversation_summaries.create_index(
        [("tenant_id", 1), ("conversation_id", 1)], unique=True
    )
    await db.conversation_summaries.create_index([("tenant_id", 1), ("updated_at", -1)])
    await db.ai_suggestions.create_index([("tenant_id", 1), ("lead_id", 1), ("status", 1)])
    await db.ai_suggestions.create_index([("lead_id", 1), ("status", 1)])
    await db.ai_usage.create_index([("tenant_id", 1), ("created_at", -1)])
    await db.ai_usage.create_index([("tenant_id", 1), ("operation", 1), ("created_at", -1)])
    await db.ai_usage.create_index([("tenant_id", 1), ("model", 1), ("created_at", -1)])
    await db.ai_usage.create_index("conversation_id")
    await db.ai_events.create_index([("tenant_id", 1), ("created_at", -1)])
    await db.ai_events.create_index([("tenant_id", 1), ("event_type", 1), ("created_at", -1)])
    await db.leads.create_index([("user_id", 1), ("current_intent", 1)])
    await db.leads.create_index([("user_id", 1), ("classified_at", -1)])

    # Stripe billing
    await db.users.create_index(
        "stripe_customer_id",
        unique=True,
        sparse=True,
    )
    await db.users.create_index(
        "stripe_subscription_id",
        unique=True,
        sparse=True,
    )
    await db.stripe_webhook_events.create_index("processed_at")
    await db.stripe_webhook_events.create_index([("status", 1), ("processing_started_at", 1)])

    logger.info("MongoDB indexes ensured")
