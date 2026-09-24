"""Platform settings loaded from environment / .env file.

`Settings` is the platform surface: the connection, provider, retrieval and
channel values every deployment needs. It carries no deployment's name and
no deployment's topology -- a consumer that needs more fields subclasses it,
and pydantic-settings reads the subclass's fields from the same environment.
`services/medallion.py` is the worked example.
"""

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
    db_name: str = "agentsys"

    # Connection URL — composed from the component vars above when those are
    # set; otherwise this default (or a directly-passed value) is used.
    database_url: str = "postgresql+asyncpg://localhost:5432/agentsys"

    # D-023 — dedicated READ-ONLY connection for the BI report tool. Point this
    # at a login role with `default_transaction_read_only = on` and a
    # `statement_timeout` (see architecture/bi-readonly-db-role). Empty means
    # the BI tool is not registered at all; it deliberately does NOT fall back
    # to `database_url`, because that engine can write and this one must not.
    bi_database_url: str = ""

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
    # #140 -- maximum accepted POST /webhook body size, enforced before HMAC
    # verification and before anything is persisted. Meta's real Cloud API
    # payloads are a few KB even for a batched envelope (media is fetched by
    # URL, never embedded), so 1 MiB is a conservative ceiling with generous
    # headroom, not a tuned-to-the-byte limit.
    webhook_max_body_bytes: int = Field(default=1_048_576, gt=0)
    meta_webhook_secret: str = ""
    meta_phone_number_id: str = ""
    meta_access_token: str = ""
    whatsapp_token: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_phone_number_id: str = ""

    # WhatsApp runtime wiring (D-014)
    # Which cached runtime an inbound WhatsApp delivery is routed to, as
    # "{deployment}__{role}". No platform default exists: the platform knows
    # no deployment names. Unset means the route resolves no runtime and
    # answers 200 without running a turn (see integration/webhook.py).
    whatsapp_runtime_id: str = ""
    whatsapp_graph_api_url: str = "https://graph.facebook.com/v21.0"
    # D-014 S4 (design AD-7) - whatsapp_checkpointer_enabled=False is a
    # deliberate OPERATOR CHOICE for configured-stateless mode (no thread_id
    # passed, no degradation logging) - distinct from AD-8's unplanned
    # runtime-failure degradation, which only applies when this is True.
    whatsapp_checkpointer_enabled: bool = True
    checkpointer_ttl_s: int | None = 86400

    # W2b2 — DeferredWebhookWorker's poll loop, started in the lifespan once
    # a runtime is resolved for whatsapp_runtime_id (see main.py::lifespan).
    # No runtime resolved means the worker never starts; durable work then
    # waits, unprocessed, rather than being dropped.
    webhook_worker_poll_interval_s: float = Field(default=1.0, gt=0)
    webhook_worker_claim_limit: int = Field(default=10, gt=0)

    # #46 (ADR-001 D-033) -- bounds how many turns may run concurrently
    # across the whole process: the webhook worker's poll loop AND
    # POST /v1/chat/completions share one TurnAdmissionLimiter built from
    # this value (see services/admission.py, main.py::lifespan). Without a
    # bound, an arriving burst opens one turn per conversation and can
    # exhaust the database pool regardless of process count. Default matches
    # ADR-001's Stage A launch target (10 concurrent conversations, single
    # process) and stays under the pool's default per-engine ceiling
    # (pool_size=5 + max_overflow=10); #45 owns tuning the pool itself.
    max_concurrent_turns: int = Field(default=10, ge=1)

    # #46 review follow-up (SHOULD-FIX 2) -- only POST /v1/chat/completions
    # ever blocks waiting for an admission slot (the webhook worker reserves
    # its slots before claiming, so a claimed row never waits -- see
    # DeferredWebhookWorker.process_available). An in-request HTTP caller
    # still needs a bound: without one, a burst at the concurrency limit
    # would hang requests indefinitely instead of failing fast. On expiry
    # the adapter answers 503 with Retry-After. Conservative default: long
    # enough to absorb a brief burst, short enough that a client is not left
    # hanging for a full turn's worth of latency.
    admission_wait_timeout_s: float = Field(default=10.0, gt=0)

    # Slack (optional, for alerts)
    slack_webhook_url: str = ""

    # OpenAI-compatible adapter (D-012)
    adapter_api_key: str = ""
    adapter_provider: Literal["ollama", "groq", "anthropic", "openai_compatible"] = (
        "ollama"
    )
    # List of model ids to expose via /v1/models. Format: "{deployment}__{role}",
    # e.g. "acme__sales-agent". Generic (no deployment) → "_generic__{role}".
    adapter_runtimes: list[str] = []

    # Any OpenAI-compatible chat endpoint (MiniMax, vLLM, LM Studio, ...),
    # selected with adapter_provider="openai_compatible".
    # Deliberately NOT named openai_* — openai_api_key above is the embeddings
    # credential, and reusing it would prevent running embeddings on OpenAI and
    # chat on another host at the same time.
    # base_url and model are REQUIRED when this provider is selected; the check
    # lives in _build_chat_model, so Settings still constructs without an env
    # file. api_key is optional — keyless local endpoints are legitimate.
    openai_compatible_api_key: str = ""
    openai_compatible_base_url: str = ""
    openai_compatible_model: str = ""

    # Ollama adapter (#169, ADR-002 E.18) — configurable model + host so a
    # role can be evaluated against a different local model (e.g. the live-eval
    # pipeline running qwen2.5:3b vs qwen3:8b) without a code change. Defaults
    # match what `_build_chat_model` hardcoded before this field existed, so
    # an unconfigured deployment's behavior is unchanged. Empty base_url means
    # "unset" -- `_build_chat_model` passes None, not "", so ChatOllama falls
    # back to its own default host resolution (OLLAMA_HOST env var, then
    # http://localhost:11434) instead of treating "" as a real base URL.
    ollama_model: str = "qwen2.5:3b"
    ollama_base_url: str = ""

    # Live-eval pipeline's own provider switch (#169 follow-up, ADR-002
    # E.18). Deliberately a SEPARATE field from `adapter_provider` above --
    # a manual eval run choosing a different model must never change what
    # the running application serves. Accepts the same values
    # `main._build_chat_model` dispatches on; `openai_compatible` then reads
    # the same `openai_compatible_*` settings as every other role (e.g.
    # OpenRouter -- see docs/platform/live-eval.md). Env var: EVAL_PROVIDER.
    eval_provider: Literal["ollama", "groq", "anthropic", "openai_compatible"] = (
        "ollama"
    )

    # App
    log_level: str = "INFO"
    debug: bool = False
    environment: str = "development"

    # D-014 S5 — security fail-closed opt-out (BLOCKER 1). Default OFF so the
    # app is secure-by-default: the guards below refuse to boot with empty
    # adapter/webhook secrets. Set ALLOW_INSECURE=true ONLY for local dev to
    # deliberately run the open/forgeable-signature modes. Do NOT reuse
    # ``debug``/``environment`` for this — the insecure switch must be explicit.
    allow_insecure: bool = False

    @model_validator(mode="after")
    def validate_security_fail_closed(self) -> "Settings":
        """Fail CLOSED at boot on insecure-secret configurations (BLOCKER 1).

        Two live fail-OPEN defaults are refused unless ``allow_insecure`` is
        explicitly enabled:

        - ``adapter_runtimes`` configured with an empty ``adapter_api_key``:
          the ``/v1/*`` endpoints would run with the runtime's full write
          grants for any unauthenticated caller.
        - an empty ``meta_webhook_secret``: HMAC-SHA256 with an empty key is
          attacker-computable, so any webhook signature is forgeable.

        Centralised here (not only in the lifespan) so the guard is
        unit-testable without spinning up FastAPI.
        """
        if self.allow_insecure:
            return self

        if self.adapter_runtimes and not self.adapter_api_key:
            raise ValueError(
                "adapter_api_key is required when adapter_runtimes is configured; "
                "set ADAPTER_API_KEY or explicitly set ALLOW_INSECURE=true for dev"
            )

        if not self.meta_webhook_secret:
            raise ValueError(
                "meta_webhook_secret is required (an empty secret makes webhook "
                "signatures forgeable); set META_WEBHOOK_SECRET or explicitly set "
                "ALLOW_INSECURE=true for dev"
            )

        return self

    @model_validator(mode="after")
    def validate_rag_thresholds(self) -> "Settings":
        if self.rag_threshold_direct <= self.rag_threshold_ambiguous:
            raise ValueError(
                "rag_threshold_direct must be greater than rag_threshold_ambiguous"
            )

        return self

    @model_validator(mode="after")
    def compose_db_urls(self) -> "Settings":
        """Build connection URLs from component vars when they are provided.

        When ``db_user`` or ``db_host`` is set (the preferred path), both
        ``database_url`` is composed from the
        component vars via ``URL.create`` — which url-encodes the password, so
        special characters never break the connection string. When the vars are
        unset, a directly-passed ``database_url`` or the default is kept as-is.

        A consumer that adds its own connections composes them in its own
        validator; see `MedallionSettings` in `services/medallion.py`.
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

        return self


@lru_cache
def get_settings() -> Settings:
    """Return a cached singleton of the application settings."""
    return Settings()
