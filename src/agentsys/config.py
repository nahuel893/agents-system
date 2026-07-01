"""Application settings loaded from environment / .env file."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """Centralised configuration using pydantic-settings.

    Values are read from environment variables first, then from a `.env`
    file at the project root.  Every field has a sensible default so the
    app can boot locally without any env file.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database — component vars (PREFERRED). Define these in .env instead of a
    # full URL; database_url is composed from them by the validator below.
    db_user: str | None = None
    db_password: str | None = None
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "badie"

    # Medallion (catalog warehouse) — overrides that fall back to the main DB
    # connection when unset (same server, different database).
    medallion_db_user: str | None = None
    medallion_db_password: str | None = None
    medallion_db_host: str | None = None
    medallion_db_port: int | None = None
    medallion_db_name: str = "medallion"

    # Connection URLs — composed from the component vars above when those are
    # set; otherwise these defaults (or a directly-passed value) are used.
    database_url: str = "postgresql+asyncpg://localhost:5432/badie"
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

    # RAG retrieval.
    # Thresholds are cosine-similarity cutoffs calibrated for the default local
    # embedder (BGE-M3) against short product descriptions. Empirically, correct
    # colloquial matches land at ~0.55–0.68 similarity, so the original
    # 0.92/0.82 cutoffs classified every real match as ``no_match``. Override per
    # environment/model via RAG_THRESHOLD_DIRECT / RAG_THRESHOLD_AMBIGUOUS.
    rag_threshold_direct: float = Field(default=0.60, gt=0, le=1)
    rag_threshold_ambiguous: float = Field(default=0.50, ge=0, lt=1)
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

    # WhatsApp runtime wiring (D-014)
    whatsapp_runtime_id: str = "badie__sales-agent"
    whatsapp_graph_api_url: str = "https://graph.facebook.com/v21.0"

    # Slack (optional, for alerts)
    slack_webhook_url: str = ""

    # OpenAI-compatible adapter (D-012)
    adapter_api_key: str = ""
    adapter_provider: Literal["ollama", "groq", "anthropic"] = "ollama"
    # List of model ids to expose via /v1/models. Format: "{deployment}__{role}",
    # e.g. "badie__sales-agent". Generic (no deployment) → "_generic__{role}".
    adapter_runtimes: list[str] = ["badie__sales-agent"]

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

    @model_validator(mode="after")
    def compose_db_urls(self) -> "Settings":
        """Build connection URLs from component vars when they are provided.

        When ``db_user`` or ``db_host`` is set (the preferred path), both
        ``database_url`` and ``medallion_database_url`` are composed from the
        component vars via ``URL.create`` — which url-encodes the password, so
        special characters never break the connection string. When the vars are
        unset, a directly-passed ``database_url`` or the default is kept as-is.

        The medallion (catalog warehouse) connection falls back to the main DB
        credentials/host/port; override only the parts that differ.
        """
        if self.db_user is None and self.db_host is None:
            return self

        self.database_url = URL.create(
            "postgresql+asyncpg",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        ).render_as_string(hide_password=False)

        self.medallion_database_url = URL.create(
            "postgresql+asyncpg",
            username=self.medallion_db_user or self.db_user,
            password=self.medallion_db_password or self.db_password,
            host=self.medallion_db_host or self.db_host,
            port=self.medallion_db_port or self.db_port,
            database=self.medallion_db_name,
        ).render_as_string(hide_password=False)

        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()
