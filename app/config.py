from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["dev", "test", "staging", "production"]

_INSECURE_JWT_SECRETS = frozenset(
    {
        "",
        "change-me",
        "change-me-to-a-long-random-string",
        "secret",
        "jwt-secret",
        "your-secret",
        "password",
        "123456",
    }
)


class Settings(BaseSettings):
    APP_ENV: str = "dev"
    PORT: int = 8000

    MONGO_URI: str = "mongodb://localhost:27017"
    MONGO_DB: str = "ai_wa_chat_agent"

    JWT_SECRET: str = "change-me"
    JWT_ALG: str = "HS256"
    JWT_EXPIRE_MIN: int = 60 * 24 * 7

    CORS_ORIGINS: str = "http://localhost:8080"
    REDIS_URL: str = "redis://localhost:6379/0"
    # Leave empty locally — Twilio rejects localhost StatusCallback URLs (21609).
    PUBLIC_BASE_URL: str = ""

    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_FROM: str = "whatsapp:+14155238886"
    # Alias also accepted via TWILIO_VALIDATE_SIGNATURES env (see property)
    TWILIO_VALIDATE_SIGNATURE: bool = True
    TWILIO_VALIDATE_SIGNATURES: bool | None = None
    TRUSTED_PROXY_COUNT: int = 1

    # WhatsApp transport selector. Default remains Twilio for all existing tenants.
    # Meta Cloud API POC is additive; set to "meta" only for explicit Meta experiments.
    WHATSAPP_PROVIDER: str = "twilio"

    # Legacy dev/test only — never production Graph send/routing authority.
    META_ACCESS_TOKEN: str = ""
    META_PHONE_NUMBER_ID: str = ""
    META_WABA_ID: str = ""
    META_APP_ID: str = ""
    META_APP_SECRET: str = ""
    META_WEBHOOK_VERIFY_TOKEN: str = ""
    META_GRAPH_VERSION: str = "v21.0"
    # When true (default), POST /api/webhook/meta/whatsapp requires valid X-Hub-Signature-256.
    # Set false only for local debugging; never disable in staging/production.
    META_WEBHOOK_VALIDATE_SIGNATURE: bool = True
    META_HTTP_TIMEOUT_SECONDS: float = 30.0
    # AES-GCM key for tenant Meta access tokens (32-byte utf-8, or base64 of 16/24/32 bytes).
    # Distinct from JWT_SECRET. Required in staging/production.
    META_TOKEN_ENCRYPTION_KEY: str = ""
    # Dev/test only: allow env META_ACCESS_TOKEN for users with meta_connection_status=legacy_poc.
    META_ALLOW_LEGACY_POC_TOKEN: bool = False
    # Facebook Login for Business Embedded Signup v4 configuration ID (App Dashboard).
    META_EMBEDDED_SIGNUP_CONFIG_ID: str = ""
    META_ONBOARDING_STATE_TTL_SECONDS: int = 600

    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_FALLBACK_MODEL: str = "gpt-4o-mini"
    OPENAI_TEMPERATURE: float = 0.5
    OPENAI_MAX_OUTPUT_TOKENS: int = 400
    OPENAI_REQUEST_TIMEOUT_SECONDS: float = 30.0
    OPENAI_MAX_RETRIES: int = 2
    OPENAI_RETRY_BASE_SECONDS: float = 1.0
    OPENAI_MAX_HISTORY: int = 20
    AI_FEATURES_ENABLED: bool = True
    AI_DEFAULT_LANGUAGE: str = "en"
    AI_ALLOWED_MODELS: str = "gpt-4o-mini,gpt-4o,gpt-4.1-mini,gpt-4.1"
    AI_SUMMARIES_ENABLED: bool = True
    AI_EXTRACTION_ENABLED: bool = True
    AI_MODERATION_ENABLED: bool = True
    AI_ANALYTICS_ENABLED: bool = True
    AI_MAX_CONTEXT_MESSAGES: int = 20
    AI_MAX_CONTEXT_CHARS: int = 12000
    AI_DAILY_TOKEN_LIMIT_PER_TENANT: int = 200000
    AI_MONTHLY_COST_LIMIT_PER_TENANT: float = 50.0
    AI_MAX_REQUESTS_PER_MINUTE_PER_TENANT: int = 30
    AI_SUMMARY_TRIGGER_MESSAGE_COUNT: int = 8
    AI_SUMMARY_MAX_OUTPUT_TOKENS: int = 250
    AI_SUMMARY_REFRESH_INTERVAL_MESSAGES: int = 6
    AI_AUTO_ESCALATE_NEGATIVE: bool = False
    AI_AUTO_ESCALATE_URGENT: bool = True
    AI_INTENT_CONFIDENCE_THRESHOLD: float = 0.55
    AI_MODERATION_PROVIDER_ENABLED: bool = False
    AI_BLOCKED_CATEGORIES: str = "threats,self_harm,illegal,prompt_injection,sexual"
    AI_MODERATION_ESCALATION_CATEGORIES: str = "threats,self_harm,illegal"
    AI_MODEL_PRICING_JSON: str = (
        '{"gpt-4o-mini":{"input_per_1m":0.15,"output_per_1m":0.60},'
        '"gpt-4o":{"input_per_1m":2.50,"output_per_1m":10.0},'
        '"gpt-4.1-mini":{"input_per_1m":0.40,"output_per_1m":1.60},'
        '"gpt-4.1":{"input_per_1m":2.0,"output_per_1m":8.0}}'
    )
    AI_FAILURE_FALLBACK_ENABLED: bool = True
    AI_FAILURE_FALLBACK_TEXT: str = (
        "Thanks for your message — a team member will follow up with you shortly."
    )
    AI_FAILURE_MARK_NEEDS_HUMAN: bool = True
    AI_LOW_CONFIDENCE_THRESHOLD: float = 0.4
    AI_MAX_RESPONSE_CHARS: int = 1200
    AI_TEST_RATE_LIMIT_PER_USER: int = 10

    MEDIA_STORAGE_DIR: str = "media_uploads"
    MEDIA_MAX_BYTES: int = 16 * 1024 * 1024
    MEDIA_STORAGE_BACKEND: str = "local"

    CAMPAIGN_BATCH_SIZE: int = 25
    CAMPAIGN_SEND_DELAY_MS: int = 200
    CAMPAIGN_MAX_RETRIES: int = 3
    CAMPAIGN_RETRY_DELAY_SECONDS: int = 60
    CAMPAIGN_REPLY_WINDOW_HOURS: int = 72
    CAMPAIGN_MAX_RECIPIENTS_PER_REQUEST: int = 5000
    BLAST_BATCH_SIZE: int = 25

    WORKER_JOB_TIMEOUT: int = 300
    WORKER_RESULT_TTL: int = 500
    WORKER_FAILURE_TTL: int = 86400

    # Rate limits (0 disables that bucket)
    RATE_LIMIT_AUTH_PER_IP: int = 20
    RATE_LIMIT_AUTH_WINDOW_SEC: int = 60
    RATE_LIMIT_SEND_PER_USER: int = 120
    RATE_LIMIT_SEND_WINDOW_SEC: int = 60
    RATE_LIMIT_UPLOAD_PER_USER: int = 30
    RATE_LIMIT_UPLOAD_WINDOW_SEC: int = 60
    RATE_LIMIT_CAMPAIGN_PER_USER: int = 20
    RATE_LIMIT_CAMPAIGN_WINDOW_SEC: int = 60
    RATE_LIMIT_WEBHOOK_PER_IP: int = 600
    RATE_LIMIT_WEBHOOK_WINDOW_SEC: int = 60
    RATE_LIMIT_STATUS_CALLBACK_PER_IP: int = 2000
    RATE_LIMIT_STATUS_CALLBACK_WINDOW_SEC: int = 60
    RATE_LIMIT_WS_CONNECTIONS_PER_USER: int = 10

    WS_MAX_MESSAGE_BYTES: int = 4096

    # Process / server (documented + used by start scripts)
    WEB_CONCURRENCY: int = 1
    WEB_TIMEOUT_SECONDS: int = 120
    KEEP_ALIVE_SECONDS: int = 75
    GRACEFUL_SHUTDOWN_SECONDS: int = 30
    # When true, API process runs the due-campaign loop (dev convenience).
    # Staging/production should run `python scheduler.py` separately and set this false.
    RUN_INLINE_SCHEDULER: bool | None = None
    SERVICE_NAME: str = "ai-wa-chat-agent-api"

    # Logging
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = ""  # auto: json in staging/production, text otherwise

    # Sentry (optional)
    SENTRY_DSN: str = ""
    SENTRY_ENVIRONMENT: str = ""
    SENTRY_RELEASE: str = ""
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    SENTRY_PROFILES_SAMPLE_RATE: float = 0.0

    # Metrics
    METRICS_ENABLED: bool = True
    METRICS_TOKEN: str = ""

    # Mongo / Redis timeouts
    MONGO_CONNECT_TIMEOUT_MS: int = 10000
    MONGO_SERVER_SELECTION_TIMEOUT_MS: int = 10000
    MONGO_MAX_POOL_SIZE: int = 50
    REDIS_CONNECT_TIMEOUT_SECONDS: int = 10
    REDIS_SOCKET_TIMEOUT_SECONDS: int = 30
    RQ_QUEUE_NAME: str = "default"
    RQ_HIGH_QUEUE_NAME: str = "high"
    RQ_DEFAULT_QUEUE_NAME: str = "default"
    RQ_BULK_QUEUE_NAME: str = "bulk"

    # Production WhatsApp sender
    TWILIO_MESSAGING_SERVICE_SID: str = ""
    TWILIO_STATUS_CALLBACK_URL: str = ""  # optional override of PUBLIC_BASE_URL-derived URL

    # Consent / STOP
    WHATSAPP_OPTOUT_KEYWORDS: str = "STOP,UNSUBSCRIBE,CANCEL,END,QUIT"
    WHATSAPP_OPTIN_KEYWORDS: str = "START,YES,UNSTOP"
    WHATSAPP_ALLOW_KEYWORD_REOPTIN: bool = True
    WHATSAPP_OPTOUT_CONFIRMATION_ENABLED: bool = True
    WHATSAPP_OPTOUT_CONFIRMATION_TEXT: str = (
        "You have been unsubscribed and will not receive further marketing messages. "
        "Reply START to opt in again."
    )

    # Throughput
    WHATSAPP_MESSAGES_PER_SECOND: float = 5.0
    WHATSAPP_MESSAGES_PER_MINUTE: int = 200
    WHATSAPP_MAX_CONCURRENT_SENDS: int = 10
    WHATSAPP_TENANT_MESSAGES_PER_MINUTE: int = 60
    WHATSAPP_QUEUE_MAX_DEPTH: int = 5000

    # Retries
    WHATSAPP_MAX_RETRIES: int = 3
    WHATSAPP_RETRY_BASE_SECONDS: int = 30
    WHATSAPP_RETRY_MAX_SECONDS: int = 900
    WHATSAPP_RETRY_JITTER_SECONDS: int = 15

    # Idempotency / reconciliation
    IDEMPOTENCY_TTL_SECONDS: int = 86400
    MESSAGE_STALE_AFTER_MINUTES: int = 60
    RECONCILIATION_BATCH_SIZE: int = 100
    SCHEDULER_LOCK_KEY: str = "locks:campaign_scheduler"
    SCHEDULER_LOCK_TTL_SECONDS: int = 90
    SCHEDULER_INTERVAL_SECONDS: int = 60

    # Lead list / import / export / bulk (E3–E6)
    LEAD_IMPORT_MAX_FILE_MB: int = 5
    LEAD_IMPORT_MAX_ROWS: int = 5000
    LEAD_IMPORT_BATCH_SIZE: int = 200
    LEAD_IMPORT_MAX_ERRORS_RETURNED: int = 50
    LEAD_EXPORT_MAX_ROWS: int = 10000
    LEAD_BULK_MAX_ITEMS: int = 200
    LEAD_LIST_DEFAULT_PAGE_SIZE: int = 25
    LEAD_LIST_MAX_PAGE_SIZE: int = 100
    LEAD_SEARCH_MAX_LENGTH: int = 100
    LEAD_OPTIONS_MAX_LIMIT: int = 50

    # Password reset / email (E7)
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30
    PASSWORD_RESET_FRONTEND_URL: str = "http://localhost:8080/reset-password"
    PASSWORD_RESET_RATE_LIMIT: int = 5
    PASSWORD_RESET_EMAIL_ENABLED: bool = False
    EMAIL_PROVIDER: str = "log"  # log | smtp | sendgrid
    EMAIL_FROM: str = "noreply@example.com"
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_USE_TLS: bool = True
    SENDGRID_API_KEY: str = ""
    AVATAR_MAX_BYTES: int = 2 * 1024 * 1024

    # Stripe billing — mode + dual env
    STRIPE_MODE: str = ""  # test | live (empty = auto: production→live, else test)
    STRIPE_ALLOW_LIVE_IN_DEV: bool = False
    FRONTEND_BASE_URL: str = "http://localhost:8080"

    STRIPE_TEST_SECRET_KEY: str = ""
    STRIPE_TEST_PUBLISHABLE_KEY: str = ""
    STRIPE_TEST_WEBHOOK_SECRET: str = ""
    STRIPE_TEST_PRICE_STARTER: str = ""
    STRIPE_TEST_PRICE_PROFESSIONAL: str = ""
    STRIPE_TEST_PRICE_BUSINESS: str = ""

    STRIPE_LIVE_SECRET_KEY: str = ""
    STRIPE_LIVE_PUBLISHABLE_KEY: str = ""
    STRIPE_LIVE_WEBHOOK_SECRET: str = ""
    STRIPE_LIVE_PRICE_STARTER: str = ""
    STRIPE_LIVE_PRICE_PROFESSIONAL: str = ""
    STRIPE_LIVE_PRICE_BUSINESS: str = ""

    # Deprecated single-slot fallbacks (migrate to STRIPE_TEST_* / STRIPE_LIVE_*)
    STRIPE_SECRET_KEY: str = ""
    STRIPE_PUBLISHABLE_KEY: str = ""
    STRIPE_WEBHOOK_SECRET: str = ""
    STRIPE_PRICE_STARTER: str = ""
    STRIPE_PRICE_PROFESSIONAL: str = ""
    STRIPE_PRICE_BUSINESS: str = ""

    STRIPE_SUCCESS_URL: str = "http://localhost:8080/billing?checkout=success"
    STRIPE_CANCEL_URL: str = "http://localhost:8080/billing?checkout=canceled"
    STRIPE_PORTAL_RETURN_URL: str = "http://localhost:8080/billing"
    STRIPE_TRIAL_DAYS: int = 14
    STRIPE_CUSTOMER_LOCK_TTL_SECONDS: int = 60
    STRIPE_WEBHOOK_CLAIM_TTL_SECONDS: int = 300
    BILLING_CONTACT_SALES_URL: str = (
        "https://calendar.google.com/calendar/u/0/appointments/schedules/"
        "AcZssZ0qcRUglD8qicU4kzrD-rFtlyP94h0JaZnv_-41rtPM-BkStaGx-mBvWG0nOP8EzQzaaMgYk8Qm"
    )

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def normalize_env(cls, v: object) -> str:
        raw = str(v or "dev").strip().lower()
        if raw in ("local", "development", "devel"):
            return "dev"
        if raw in ("prod", "prd"):
            return "production"
        if raw in ("stage", "stg"):
            return "staging"
        if raw not in ("dev", "test", "staging", "production"):
            raise ValueError(
                f"APP_ENV must be one of: dev, test, staging, production (got {raw!r})"
            )
        return raw

    @property
    def app_env(self) -> AppEnv:
        return self.APP_ENV  # type: ignore[return-value]

    @property
    def is_production_like(self) -> bool:
        return self.app_env in ("staging", "production")

    @property
    def is_dev_or_test(self) -> bool:
        return self.app_env in ("dev", "test")

    @property
    def twilio_validate_signatures(self) -> bool:
        if self.TWILIO_VALIDATE_SIGNATURES is not None:
            return bool(self.TWILIO_VALIDATE_SIGNATURES)
        return bool(self.TWILIO_VALIDATE_SIGNATURE)

    @property
    def whatsapp_provider(self) -> str:
        raw = (self.WHATSAPP_PROVIDER or "twilio").strip().lower()
        return raw if raw in ("twilio", "meta") else "twilio"

    @property
    def embedded_signup_available(self) -> bool:
        """True only when the API can complete Embedded Signup (secret stays server-side)."""
        return bool(
            (self.META_APP_ID or "").strip()
            and (self.META_APP_SECRET or "").strip()
            and (self.META_EMBEDDED_SIGNUP_CONFIG_ID or "").strip()
        )

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def twilio_status_callback_url(self) -> str | None:
        """Public URL Twilio will POST delivery status updates to, or None if unset."""
        explicit = (self.TWILIO_STATUS_CALLBACK_URL or "").strip()
        if explicit:
            return explicit.rstrip("/")
        base = (self.PUBLIC_BASE_URL or "").strip().rstrip("/")
        if not base:
            return None
        return f"{base}/api/webhook/twilio/status"

    @property
    def optout_keywords(self) -> set[str]:
        return {k.strip().lower() for k in self.WHATSAPP_OPTOUT_KEYWORDS.split(",") if k.strip()}

    @property
    def optin_keywords(self) -> set[str]:
        return {k.strip().lower() for k in self.WHATSAPP_OPTIN_KEYWORDS.split(",") if k.strip()}

    @property
    def run_inline_scheduler(self) -> bool:
        if self.RUN_INLINE_SCHEDULER is not None:
            return bool(self.RUN_INLINE_SCHEDULER)
        # Default: inline only in local/dev/test to avoid duplicate schedulers under multi-worker web.
        return self.is_dev_or_test

    @property
    def log_format(self) -> str:
        raw = (self.LOG_FORMAT or "").strip().lower()
        if raw in ("json", "text"):
            return raw
        return "json" if self.is_production_like else "text"

    @property
    def sentry_environment(self) -> str:
        return (self.SENTRY_ENVIRONMENT or self.APP_ENV or "dev").strip()


    def validate_for_startup(self) -> None:
        """Fail fast on insecure / incomplete production configuration."""
        errors: list[str] = []

        if self.is_production_like:
            required = {
                "MONGO_URI": self.MONGO_URI,
                "MONGO_DB": self.MONGO_DB,
                "JWT_SECRET": self.JWT_SECRET,
                "REDIS_URL": self.REDIS_URL,
                "TWILIO_ACCOUNT_SID": self.TWILIO_ACCOUNT_SID,
                "TWILIO_AUTH_TOKEN": self.TWILIO_AUTH_TOKEN,
                "TWILIO_WHATSAPP_FROM": self.TWILIO_WHATSAPP_FROM,
                "PUBLIC_BASE_URL": self.PUBLIC_BASE_URL,
            }
            for name, value in required.items():
                if not (value or "").strip():
                    errors.append(f"{name} is required when APP_ENV={self.app_env}")

            secret = (self.JWT_SECRET or "").strip()
            if secret.lower() in _INSECURE_JWT_SECRETS or len(secret) < 32:
                errors.append(
                    "JWT_SECRET must be a strong secret (min 32 chars) in staging/production"
                )

            if self.JWT_ALG.upper() not in ("HS256", "HS384", "HS512"):
                errors.append(f"JWT_ALG {self.JWT_ALG!r} is not allowed")

            pub = (self.PUBLIC_BASE_URL or "").strip()
            if pub:
                parsed = urlparse(pub)
                if parsed.scheme != "https" or not parsed.netloc:
                    errors.append("PUBLIC_BASE_URL must be an https:// URL in staging/production")

            origins = self.cors_origins_list
            if not origins:
                errors.append("CORS_ORIGINS must list at least one exact origin in staging/production")
            if any(o == "*" for o in origins):
                errors.append("CORS_ORIGINS must not use '*' when credentials are enabled")

            if not self.twilio_validate_signatures:
                errors.append(
                    "TWILIO_VALIDATE_SIGNATURE / TWILIO_VALIDATE_SIGNATURES must be true "
                    f"when APP_ENV={self.app_env}"
                )

            if not self.META_WEBHOOK_VALIDATE_SIGNATURE:
                errors.append(
                    "META_WEBHOOK_VALIDATE_SIGNATURE must be true "
                    f"when APP_ENV={self.app_env}"
                )

            if self.META_ALLOW_LEGACY_POC_TOKEN:
                errors.append(
                    "META_ALLOW_LEGACY_POC_TOKEN must be false "
                    f"when APP_ENV={self.app_env}"
                )

            enc = (self.META_TOKEN_ENCRYPTION_KEY or "").strip()
            if not enc:
                errors.append(
                    "META_TOKEN_ENCRYPTION_KEY is required "
                    f"when APP_ENV={self.app_env}"
                )
            elif enc == (self.JWT_SECRET or "").strip():
                errors.append("META_TOKEN_ENCRYPTION_KEY must not equal JWT_SECRET")
            else:
                try:
                    from app.services.meta_credentials import MetaCredentialsError, load_encryption_key

                    load_encryption_key(enc)
                except MetaCredentialsError as exc:
                    errors.append(str(exc))

            if not (self.META_APP_ID or "").strip():
                errors.append(
                    f"META_APP_ID is required when APP_ENV={self.app_env}"
                )
            if not (self.META_APP_SECRET or "").strip():
                errors.append(
                    f"META_APP_SECRET is required when APP_ENV={self.app_env}"
                )
            if not (self.META_WEBHOOK_VERIFY_TOKEN or "").strip():
                errors.append(
                    f"META_WEBHOOK_VERIFY_TOKEN is required when APP_ENV={self.app_env}"
                )

            if self.AI_FEATURES_ENABLED and not (self.OPENAI_API_KEY or "").strip():
                errors.append("OPENAI_API_KEY is required when AI_FEATURES_ENABLED=true")

            redis_url = (self.REDIS_URL or "").strip().lower()
            if redis_url.startswith("redis://") and self.app_env == "production":
                errors.append(
                    "REDIS_URL should use TLS (rediss://) in production when supported"
                )
            elif redis_url.startswith("redis://") and self.app_env == "staging":
                import logging

                logging.getLogger("app.config").warning(
                    "REDIS_URL is not using TLS (rediss://) in staging"
                )

            if self.MEDIA_STORAGE_BACKEND not in ("local",):
                # Cloud backends would require their own credentials — keep local for now.
                errors.append(
                    f"MEDIA_STORAGE_BACKEND={self.MEDIA_STORAGE_BACKEND!r} is not supported yet"
                )

            if self.METRICS_ENABLED and not (self.METRICS_TOKEN or "").strip():
                errors.append(
                    "METRICS_TOKEN is required when METRICS_ENABLED=true in staging/production"
                )

            if self.RUN_INLINE_SCHEDULER is True:
                errors.append(
                    "RUN_INLINE_SCHEDULER must be false in staging/production "
                    "(run python scheduler.py as its own service)"
                )

        # Stripe (always evaluate; production rules are stricter inside)
        try:
            from app.billing.stripe_config import clear_stripe_runtime_cache, validate_stripe_configuration

            clear_stripe_runtime_cache()
            errors.extend(validate_stripe_configuration(for_startup=True))
        except Exception as exc:  # pragma: no cover
            errors.append(f"Stripe configuration validation error: {exc}")

        # Always validate CORS shape (even in dev) for wildcards with credentials
        if "*" in self.cors_origins_list:
            errors.append("CORS_ORIGINS cannot include '*' (credentials are enabled)")

        if errors:
            joined = "; ".join(errors)
            raise RuntimeError(f"Configuration validation failed: {joined}")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
