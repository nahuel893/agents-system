"""Application settings loaded from environment / .env file."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralised configuration using pydantic-settings.

    Values are read from environment variables first, then from a `.env`
    file at the project root.  Every field has a sensible default so the
    app can boot locally without any env file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # Database
    database_url: str = "postgresql+asyncpg://localhost:5432/badie"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # LLM - Anthropic
    anthropic_api_key: str = ""

    # Embeddings - OpenAI
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 512

    # WhatsApp / Meta
    meta_webhook_secret: str = ""
    meta_phone_number_id: str = ""
    meta_access_token: str = ""
    whatsapp_token: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_phone_number_id: str = ""

    # Slack (optional, for alerts)
    slack_webhook_url: str = ""

    # App
    log_level: str = "INFO"
    debug: bool = False
    environment: str = "development"


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()
