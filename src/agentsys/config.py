"""Application settings loaded from environment / .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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

    # Medallion data warehouse (read-only, source of truth for catalog/clients)
    medallion_database_url: str = "postgresql+asyncpg://localhost:5432/medallion"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # LLM - Anthropic
    anthropic_api_key: str = ""

    # Embeddings — provider switch (local default = no API key required)
    embedding_provider: Literal["openai", "local"] = "local"
    embedding_dimensions: int = 512

    # Embeddings — OpenAI
    openai_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"

    # Embeddings — local (sentence-transformers)
    embedding_model_local: str = "BAAI/bge-m3"

    # RAG retrieval
    rag_threshold_direct: float = Field(default=0.92, gt=0, le=1)
    rag_threshold_ambiguous: float = Field(default=0.82, ge=0, lt=1)
    rag_top_k: int = Field(default=3, gt=0)
    rag_keyword_top_k: int = Field(default=5, gt=0)
    rag_hnsw_ef_search: int = Field(default=40, gt=0)

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

    @model_validator(mode="after")
    def validate_rag_thresholds(self) -> "Settings":
        if self.rag_threshold_direct <= self.rag_threshold_ambiguous:
            raise ValueError(
                "rag_threshold_direct must be greater than "
                "rag_threshold_ambiguous"
            )

        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()
