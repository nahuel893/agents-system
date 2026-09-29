"""Platform settings loaded from environment / .env file.

`Settings` is the platform surface: the connection, provider, retrieval and
channel values every deployment needs. It carries no deployment's name and
no deployment's topology -- a consumer that needs more fields subclasses it,
and pydantic-settings reads the subclass's fields from the same environment.
`services/medallion.py` is the worked example.
"""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agents_system.extras import raise_missing_extra

#: Review finding 6 (PR #87) -- a generous but finite sanity ceiling on a
#: per-million-token price. No real hosted provider prices anywhere near
#: this; it exists only to reject a malformed config (a misplaced decimal,
#: a unit mix-up) at boot rather than silently computing an absurd cost.
_MAX_PRICE_PER_MILLION = 10_000.0

# The main lifespan starts two background users of the operational engine:
# AuditSink's drainer and DeferredWebhookWorker's poll loop. This headroom is
# reserved in addition to concurrent turns so either worker can check out a
# connection while every admitted turn holds its one connection for a round.
_OPERATIONAL_ENGINE_BACKGROUND_CONNECTION_HEADROOM = 2


class ModelPrice(BaseModel):
    """Cost per million tokens for one model id (issue #78 Phase 0).

    Both prices are USD per 1,000,000 tokens -- the unit every hosted
    provider quotes in, so a value here can be copy-pasted from a pricing
    page without conversion.

    Review finding 6 (PR #87): `extra="forbid"` so a typo'd key (e.g.
    `input_tokens_per_million`) fails loudly instead of being silently
    ignored while a required field stays missing; `allow_inf_nan=False` plus
    a finite upper bound so `Infinity`/`NaN`/an absurd value (`1e308`) can
    never make `cost_usd` compute as `inf`/`nan` -- a malformed price is
    refused at boot, never turned into a meaningless number later.
    """

    model_config = ConfigDict(extra="forbid")

    input_per_million: float = Field(
        ge=0, le=_MAX_PRICE_PER_MILLION, allow_inf_nan=False
    )
    output_per_million: float = Field(
        ge=0, le=_MAX_PRICE_PER_MILLION, allow_inf_nan=False
    )


class ProviderSettings(BaseSettings):
    """The chat-model provider settings, and nothing else (issue #186).

    The library builds a chat model from an agent's declared ``model`` without
    the server: ``agent.models.build_chat_model`` reads the provider host,
    model and credentials from THIS class. The full ``Settings`` below inherits
    from it, so the served app and the library read the same variables with the
    same defaults from one definition -- but constructing ``ProviderSettings``
    runs none of the server's fail-closed validators (#167), so it works with
    no ``META_WEBHOOK_SECRET``, no database URL and no ``.env`` at all.

    The hosted providers' keys (``GROQ_API_KEY``, ``ANTHROPIC_API_KEY``) are
    read straight from the process environment by ``build_chat_model``, as
    they always were.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Any OpenAI-compatible chat endpoint (MiniMax, vLLM, LM Studio, ...),
    # selected with adapter_provider="openai_compatible".
    # Deliberately NOT named openai_* — Settings.openai_api_key is the embeddings
    # credential, and reusing it would prevent running embeddings on OpenAI and
    # chat on another host at the same time.
    # base_url and model are REQUIRED when this provider is selected; the check
    # lives in _build_chat_model, so Settings still constructs without an env
    # file. api_key is optional — keyless local endpoints are legitimate.
    openai_compatible_api_key: str = ""
    openai_compatible_base_url: str = ""
    openai_compatible_model: str = ""
    # issue #145 -- bounded per-HTTP-call timeout to the openai_compatible
    # provider. Previously unset, which left the openai SDK's own default in
    # effect (connect=5s, read/write/pool=600s): one stuck call could wait up
    # to ten minutes with no visible failure. 45s is comfortably under
    # the platform default total_execution_timeout_s (60s,
    # harness.loader.PLATFORM_DEFAULT_LIMITS)
    # so a single hung call fails with a clear client-side timeout instead of
    # being silently absorbed by the platform's own per-turn budget.
    openai_compatible_request_timeout_s: float = Field(default=45.0, gt=0)

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


class Settings(ProviderSettings):
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
    db_name: str = "agents_system"

    # Connection URL — composed from the component vars above when those are
    # set; otherwise this default (or a directly-passed value) is used.
    database_url: str = "postgresql+asyncpg://localhost:5432/agents_system"

    # D-023 — dedicated READ-ONLY connection for the BI report tool. Point this
    # at a login role with `default_transaction_read_only = on` and a
    # `statement_timeout` (see architecture/bi-readonly-db-role). Empty means
    # the BI tool is not registered at all; it deliberately does NOT fall back
    # to `database_url`, because that engine can write and this one must not.
    bi_database_url: str = ""

    # Database connection pools. Each engine has a separate pool in every
    # process, so their maximum connection counts add together. ``max_overflow``
    # deliberately permits zero: the SQL tool must not burst beyond its small,
    # dedicated pool. The remaining limits must be positive to fail malformed
    # deployment configuration at startup instead of during load.
    database_pool_size: int = Field(default=5, gt=0)
    database_max_overflow: int = Field(default=10, ge=0)
    database_pool_timeout_s: int = Field(default=30, gt=0)
    database_pool_recycle_s: int = Field(default=1_800, gt=0)

    bi_pool_size: int = Field(default=3, gt=0)
    bi_max_overflow: int = Field(default=0, ge=0)
    bi_pool_timeout_s: int = Field(default=30, gt=0)
    bi_pool_recycle_s: int = Field(default=1_800, gt=0)

    medallion_pool_size: int = Field(default=3, gt=0)
    medallion_max_overflow: int = Field(default=0, ge=0)
    medallion_pool_timeout_s: int = Field(default=30, gt=0)
    medallion_pool_recycle_s: int = Field(default=1_800, gt=0)

    sql_pool_size: int = Field(default=2, gt=0)
    sql_max_overflow: int = Field(default=0, ge=0)
    sql_pool_timeout_s: int = Field(default=30, gt=0)
    sql_pool_recycle_s: int = Field(default=1_800, gt=0)

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
    # Which cached runtime an inbound WhatsApp delivery is routed to: a
    # registered runtime id (ADR-004 D5), looked up as an opaque key and
    # never parsed. No platform default exists: the platform knows no
    # deployment names. Unset means the route resolves no runtime and
    # answers 200 without running a turn (see integration/webhook.py).
    whatsapp_runtime_id: str = ""
    whatsapp_graph_api_url: str = "https://graph.facebook.com/v21.0"
    # D-014 S4 (design AD-7) - whatsapp_checkpointer_enabled=False is a
    # deliberate OPERATOR CHOICE for configured-stateless mode (no thread_id
    # passed, no degradation logging) - distinct from AD-8's unplanned
    # runtime-failure degradation, which only applies when this is True.
    whatsapp_checkpointer_enabled: bool = True

    # Issue #24 -- the checkpointer keeps each conversation thread in the
    # platform's Postgres database (the same `database_url`), so a restart or
    # a container recreation no longer loses it. How long an IDLE thread is
    # kept is a separate, business-owned value: a periodic purge deletes every
    # thread whose last turn is older than `checkpointer_retention_s`, and
    # prunes each thread's superseded checkpoints. The default keeps the
    # previous 24-hour window; `CHECKPOINTER_RETENTION_S=never` keeps threads
    # indefinitely (a deliberate choice, see docs/platform/conversation-memory.md).
    checkpointer_retention_s: int | None = Field(default=86_400, gt=0)
    checkpointer_purge_interval_s: float = Field(default=3_600.0, gt=0)
    # The checkpointer's own connection pool (psycopg, not the SQLAlchemy
    # engine), so its connections add to the database's total. The timeout
    # bounds both taking a connection and each statement: a checkpointer that
    # cannot answer in time degrades the turn instead of stalling it.
    checkpointer_pool_size: int = Field(default=5, gt=0)
    checkpointer_timeout_s: float = Field(default=5.0, gt=0)
    # Deprecated: the Redis checkpointer's TTL, read as the retention it
    # always amounted to so an upgrade keeps a deployment's chosen window.
    checkpointer_ttl_s: int | None = Field(default=None, gt=0)

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
    # #37 -- maximum accepted POST /v1/chat/completions body size, checked
    # before JSON parsing (`integration.body_limits.read_bounded_body`, the
    # same primitive #140's webhook_max_body_bytes guard uses). This route
    # can be reached with NO authentication at all whenever adapter_api_key
    # is unset (verify_bearer is a no-op dependency in that mode), so the
    # size ceiling is the only thing standing between an anonymous caller
    # and an unbounded read. Same 1 MiB default and rationale as
    # webhook_max_body_bytes: generous headroom over any real chat-completion
    # payload, not tuned to the byte.
    adapter_max_body_bytes: int = Field(default=1_048_576, gt=0)
    adapter_api_key: str = ""
    adapter_provider: Literal["ollama", "groq", "anthropic", "openai_compatible"] = (
        "ollama"
    )
    # The registered runtime ids /v1/models publishes (ADR-004 D5), e.g.
    # ["acme-sales"]. Each is an opaque key into the registration, never
    # parsed for a role or a deployment.
    adapter_runtimes: list[str] = []

    # ADR-004 D5 -- the runtimes main.py's lifespan builds when create_app
    # got no `agents=`. Maps each deployer-chosen runtime id to the
    # predefined role it serves, as "{role}" or "{role}@{client}" (client =
    # the deployments/<client>/<role>/ override). The id is opaque; the value
    # has exactly one optional "@" and is parsed strictly by
    # main._parse_agent_registration -- a malformed entry fails boot.
    # Replaces the "{deployment}__{role}" encoding ADAPTER_RUNTIMES and
    # WHATSAPP_RUNTIME_ID used to carry. Ignored when create_app gets
    # `agents=`. e.g.
    # AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@acme", "support": "sales-agent"}'
    agent_registrations: dict[str, str] = {}

    # permission-model PR3 (issue #38, design.md Resolved Decision 5) — the
    # SOLE grant source for main.py's lifespan when create_app got no
    # `grants=`. Keyed by the registered runtime id (the same opaque ids as
    # AGENT_REGISTRATIONS/ADAPTER_RUNTIMES/WHATSAPP_RUNTIME_ID), valued by
    # the wire-name permission strings actually granted to that runtime.
    # Replaces AD-5's auto-grant-of-the-role's-full-permission-set: a role's
    # own `definition.permissions` is what the role DECLARES it may need,
    # never what a deployment GRANTS it. Empty by default; main.py fails
    # boot loudly (DefinitionError) for any registered runtime with no
    # matching entry here -- there is no automatic grant.
    # e.g. DEPLOY_GRANTS='{"acme-sales": ["read:catalog", "write:orders"]}'
    deploy_grants: dict[str, tuple[str, ...]] = {}

    # #78 Phase 0 -- cost table for real per-turn token usage. Keyed by the
    # PROVIDER model id that bills the tokens (`agent/graph.py`'s
    # `model_display_name(model)`, e.g. "deepseek/deepseek-v4-flash"), never
    # by a runtime id: the OpenAI adapter and the WhatsApp webhook worker
    # both rely on that derived key (see docs/platform/live-eval.md). A model
    # id with no entry here means cost is honestly `None` -- `agent/graph.py`
    # never guesses a price. e.g.
    # MODEL_PRICES='{"deepseek/deepseek-v4-flash": {"input_per_million": 0.14, "output_per_million": 0.28}}'
    model_prices: dict[str, ModelPrice] = {}

    # #78 Phase 0 Slice 2 -- GET /metrics (Prometheus text format,
    # `main.py`'s `metrics()` route, `observability/metrics.py`). Off by
    # default: an operator must explicitly opt in, the same fail-closed
    # posture as adapter_runtimes/adapter_api_key above -- an unauthenticated
    # process/turn/tool metrics endpoint is itself a minor information
    # disclosure (deployment shape, call volume), so it stays absent
    # (404, not merely unauthenticated) until turned on.
    metrics_enabled: bool = False
    # Optional bearer token protecting /metrics once enabled (same
    # constant-time-compare mechanism as adapter_api_key/verify_bearer in
    # integration/openai_adapter.py). Empty + metrics_enabled means an open,
    # unauthenticated /metrics -- refused at boot unless allow_insecure is
    # also set (see validate_security_fail_closed below).
    metrics_api_key: str = ""

    # Live-eval pipeline's own provider switch (#169 follow-up, ADR-002
    # E.18). Deliberately a SEPARATE field from `adapter_provider` above --
    # a manual eval run choosing a different model must never change what
    # the running application serves. Accepts the same values
    # `main._build_chat_model` dispatches on; `openai_compatible` then reads
    # the same `openai_compatible_*` settings as every other role (e.g.
    # OpenRouter -- see docs/platform/live-eval.md). Env var: EVAL_PROVIDER.
    #
    # No default (#182): it used to be "ollama", so a live run that forgot the
    # variable silently measured a local model. Unset now means
    # ``build_eval_model`` raises a clear error before any model is built.
    eval_provider: (
        Literal["ollama", "groq", "anthropic", "openai_compatible"] | None
    ) = None

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
        - ``metrics_enabled`` with an empty ``metrics_api_key`` (#78 Phase 0
          Slice 2): ``GET /metrics`` would be open to anyone.
        - an empty ``meta_webhook_secret``: HMAC-SHA256 with an empty key is
          attacker-computable, so any webhook signature is forgeable.
        - an empty ``whatsapp_verify_token`` while the WhatsApp channel is
          enabled (``whatsapp_runtime_id`` set): the GET handshake compares
          the supplied token with it, and two empty strings match, so the
          endpoint would answer anyone (#182).

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

        if self.metrics_enabled and not self.metrics_api_key:
            raise ValueError(
                "metrics_api_key is required when metrics_enabled is set; "
                "set METRICS_API_KEY or explicitly set ALLOW_INSECURE=true for dev"
            )

        if not self.meta_webhook_secret:
            raise ValueError(
                "meta_webhook_secret is required (an empty secret makes webhook "
                "signatures forgeable); set META_WEBHOOK_SECRET or explicitly set "
                "ALLOW_INSECURE=true for dev"
            )

        if self.whatsapp_runtime_id and not self.whatsapp_verify_token:
            raise ValueError(
                "whatsapp_verify_token is required when the WhatsApp channel is "
                "enabled (whatsapp_runtime_id is set); an empty token matches an "
                "empty hub.verify_token, so anyone could complete the webhook "
                "handshake. Set WHATSAPP_VERIFY_TOKEN or explicitly set "
                "ALLOW_INSECURE=true for dev"
            )

        return self

    @model_validator(mode="after")
    def validate_operational_pool_capacity(self) -> "Settings":
        """Ensure admitted turns and in-process workers fit one engine pool."""
        pool_ceiling = self.database_pool_size + self.database_max_overflow
        required_capacity = (
            self.max_concurrent_turns
            + _OPERATIONAL_ENGINE_BACKGROUND_CONNECTION_HEADROOM
        )
        if pool_ceiling < required_capacity:
            raise ValueError(
                "database_pool_size + database_max_overflow must be at least "
                "max_concurrent_turns + 2 (two in-process background workers "
                "share the operational engine)"
            )
        return self

    @model_validator(mode="after")
    def validate_rag_thresholds(self) -> "Settings":
        if self.rag_threshold_direct <= self.rag_threshold_ambiguous:
            raise ValueError(
                "rag_threshold_direct must be greater than rag_threshold_ambiguous"
            )

        return self

    @field_validator("checkpointer_retention_s", mode="before")
    @classmethod
    def parse_retention_never(cls, value: object) -> object:
        """``never`` (any case) means keep idle conversations indefinitely.

        An environment variable cannot express ``None``; a word can, and
        unlike ``0`` it cannot be misread as "delete immediately".
        """
        if isinstance(value, str) and value.strip().lower() == "never":
            return None
        return value

    @model_validator(mode="after")
    def apply_legacy_checkpointer_ttl(self) -> "Settings":
        """Read the deprecated ``CHECKPOINTER_TTL_S`` as the retention.

        Setting both is refused rather than silently preferring one: the two
        names describe the same window, so two values are a mistake.
        """
        if self.checkpointer_ttl_s is None:
            return self
        if "checkpointer_retention_s" in self.model_fields_set:
            raise ValueError(
                "CHECKPOINTER_TTL_S is deprecated and CHECKPOINTER_RETENTION_S "
                "replaces it; set only CHECKPOINTER_RETENTION_S"
            )
        self.checkpointer_retention_s = self.checkpointer_ttl_s
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

        # The database is the `postgres` extra; ModelPrice and the provider
        # settings above are core and must import without it (issue #188).
        try:
            from sqlalchemy import URL
        except ImportError as exc:
            raise_missing_extra("postgres", "Composing DATABASE_URL", exc)

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
