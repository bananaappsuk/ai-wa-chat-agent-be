from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_WHATSAPP_FROM: str = "whatsapp:+14155238886"
    TWILIO_VALIDATE_SIGNATURE: bool = True

    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_MAX_HISTORY: int = 20

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
