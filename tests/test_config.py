"""Tests for agents_system.config — Settings loading and singleton."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agents_system.config import ModelPrice, Settings, get_settings


def test_settings_loads_with_defaults():
    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.debug is False
    assert "postgresql" in settings.database_url
    assert settings.rag_threshold_direct == 0.60
    assert settings.rag_threshold_ambiguous == 0.50
    assert settings.rag_top_k == 3
    assert settings.rag_keyword_top_k == 5
    assert settings.rag_hnsw_ef_search == 40


def test_rag_thresholds_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_THRESHOLD_DIRECT", "0.70")
    monkeypatch.setenv("RAG_THRESHOLD_AMBIGUOUS", "0.55")
    settings = Settings(_env_file=None)
    assert settings.rag_threshold_direct == 0.70
    assert settings.rag_threshold_ambiguous == 0.55


@pytest.mark.parametrize(
    ("direct", "ambiguous"),
    [
        (0.82, 0.82),
        (0.81, 0.82),
    ],
)
def test_settings_reject_invalid_rag_threshold_order(
    direct: float, ambiguous: float
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            rag_threshold_direct=direct,
            rag_threshold_ambiguous=ambiguous,
        )

    assert "rag_threshold_direct" in str(excinfo.value)
    assert "rag_threshold_ambiguous" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("rag_top_k", 0),
        ("rag_keyword_top_k", -1),
        ("rag_hnsw_ef_search", 0),
    ],
)
def test_settings_reject_non_positive_rag_limits(field_name: str, value: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **{field_name: value})

    assert field_name in str(excinfo.value)


# ---------------------------------------------------------------------------
# #46 (ADR-001 D-033) — admission control setting
# ---------------------------------------------------------------------------


def test_max_concurrent_turns_has_a_conservative_positive_default():
    settings = Settings(_env_file=None)
    assert settings.max_concurrent_turns >= 1


def test_max_concurrent_turns_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_CONCURRENT_TURNS", "3")
    settings = Settings(_env_file=None)
    assert settings.max_concurrent_turns == 3


@pytest.mark.parametrize("value", [0, -1])
def test_settings_reject_non_positive_max_concurrent_turns(value: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, max_concurrent_turns=value)

    assert "max_concurrent_turns" in str(excinfo.value)


# ---------------------------------------------------------------------------
# #46 review follow-up (SHOULD-FIX 2) — admission wait timeout setting
# ---------------------------------------------------------------------------


def test_admission_wait_timeout_s_has_a_conservative_positive_default():
    settings = Settings(_env_file=None)
    assert settings.admission_wait_timeout_s > 0


def test_admission_wait_timeout_s_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADMISSION_WAIT_TIMEOUT_S", "2.5")
    settings = Settings(_env_file=None)
    assert settings.admission_wait_timeout_s == 2.5


@pytest.mark.parametrize("value", [0, -1])
def test_settings_reject_non_positive_admission_wait_timeout_s(value: float) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, admission_wait_timeout_s=value)

    assert "admission_wait_timeout_s" in str(excinfo.value)


def test_get_settings_returns_singleton():
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


# ---------------------------------------------------------------------------
# D-012 — adapter config fields
# ---------------------------------------------------------------------------


def test_adapter_config_defaults():
    """adapter_api_key, adapter_provider, and adapter_runtimes have correct defaults.

    `adapter_runtimes` is empty because runtime ids are "{deployment}__{role}"
    and the platform knows no deployment names. It also means the default
    configuration exposes no runtime at all through `/v1/*`, which is the
    safer end of the change: the fail-closed guard below still refuses the
    moment a runtime IS configured without a key.
    """
    settings = Settings(_env_file=None)
    assert settings.adapter_api_key == ""
    assert settings.adapter_provider == "ollama"
    assert settings.adapter_runtimes == []


# ---------------------------------------------------------------------------
# permission-model PR3 (issue #38, design.md Resolved Decision 5) —
# Settings.deploy_grants
# ---------------------------------------------------------------------------


def test_deploy_grants_defaults_to_empty_dict() -> None:
    """An unset DEPLOY_GRANTS yields an empty deploy_grants -- no exception
    at Settings construction time. The boot-time failure for a specific
    missing model_id is main.py's lifespan concern, not this field's own
    validation (spec: 'Boot failure without an explicit grant')."""
    settings = Settings(_env_file=None)
    assert settings.deploy_grants == {}


def test_deploy_grants_parses_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """DEPLOY_GRANTS is a JSON object mapping runtime id -> list of wire
    names, decoded via pydantic-settings' existing JSON-env mechanism (the
    same one adapter_runtimes above already uses for a structured field)."""
    monkeypatch.setenv(
        "DEPLOY_GRANTS",
        '{"whatsapp__sales-agent": ["read:catalog", "write:orders"]}',
    )
    settings = Settings(_env_file=None)
    assert settings.deploy_grants == {
        "whatsapp__sales-agent": ("read:catalog", "write:orders")
    }


def test_deploy_grants_accepts_direct_construction() -> None:
    """Direct kwarg construction (as tests/main.py build settings) accepts a
    plain dict[str, list[str]] and normalizes values to tuples."""
    settings = Settings(
        _env_file=None,
        deploy_grants={"_generic__sales-agent": ["read:catalog"]},
    )
    assert settings.deploy_grants == {"_generic__sales-agent": ("read:catalog",)}


# ---------------------------------------------------------------------------
# #78 Phase 0 — Settings.model_prices
# ---------------------------------------------------------------------------


def test_model_prices_defaults_to_empty_dict() -> None:
    """An unset MODEL_PRICES yields an empty model_prices -- cost stays
    honestly None for every model id until one is explicitly configured."""
    settings = Settings(_env_file=None)
    assert settings.model_prices == {}


def test_model_prices_parses_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """MODEL_PRICES is a JSON object mapping model id -> {input,output} price
    per million tokens, decoded via pydantic-settings' existing JSON-env
    mechanism (the same one deploy_grants above already uses)."""
    monkeypatch.setenv(
        "MODEL_PRICES",
        '{"acme__sales-agent": '
        '{"input_per_million": 0.14, "output_per_million": 0.28}}',
    )
    settings = Settings(_env_file=None)
    assert settings.model_prices == {
        "acme__sales-agent": ModelPrice(input_per_million=0.14, output_per_million=0.28)
    }


def test_model_prices_accepts_direct_construction() -> None:
    """Direct kwarg construction accepts a plain dict[str, dict[str, float]]
    and normalizes values to ModelPrice."""
    settings = Settings(
        _env_file=None,
        model_prices={
            "acme__sales-agent": {
                "input_per_million": 1.0,
                "output_per_million": 2.0,
            }
        },
    )
    assert settings.model_prices == {
        "acme__sales-agent": ModelPrice(input_per_million=1.0, output_per_million=2.0)
    }


def test_model_prices_rejects_negative_price() -> None:
    """A negative price is refused at construction time -- fails loud rather
    than silently computing a negative cost later."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            model_prices={
                "acme__sales-agent": {
                    "input_per_million": -1.0,
                    "output_per_million": 2.0,
                }
            },
        )


def test_model_prices_rejects_unknown_fields() -> None:
    """Review finding 6 (PR #87) -- `ModelPrice` forbids extra keys, so a
    typo (e.g. `input_tokens_per_million`) fails loudly instead of being
    silently ignored while a required field stays missing."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            model_prices={
                "m": {
                    "input_per_million": 1.0,
                    "output_per_million": 2.0,
                    "unknown_field": "val",
                }
            },
        )


def test_model_prices_rejects_infinite_price() -> None:
    """Review finding 6 (PR #87) -- `Infinity` satisfies `ge=0` (Python's
    own `float('inf') >= 0` is True) but must still be rejected: an infinite
    price would make `cost_usd` compute as `inf`/`nan`, a plausible-looking
    but meaningless number rather than the honest `None`."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            model_prices={
                "m": {
                    "input_per_million": float("inf"),
                    "output_per_million": 1.0,
                }
            },
        )


def test_model_prices_rejects_nan_price() -> None:
    """Review finding 6 (PR #87) -- same reasoning as the infinity case."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            model_prices={
                "m": {
                    "input_per_million": float("nan"),
                    "output_per_million": 1.0,
                }
            },
        )


def test_model_prices_rejects_unreasonably_large_price() -> None:
    """Review finding 6 (PR #87) -- a finite but absurd price (`1e308`) is
    still a malformed configuration, not a real per-million-token rate."""
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            model_prices={
                "m": {
                    "input_per_million": 1e308,
                    "output_per_million": 1.0,
                }
            },
        )


# ---------------------------------------------------------------------------
# #169 (ADR-002 E.18) — configurable Ollama model + base URL
# ---------------------------------------------------------------------------


def test_ollama_config_defaults_match_the_previous_hardcoded_values() -> None:
    """ollama_model/ollama_base_url must default to what `_build_chat_model`
    hardcoded before #169, so making the provider configurable changes no
    consumer's behavior until they actually set an env var.
    """
    settings = Settings(_env_file=None)
    assert settings.ollama_model == "qwen2.5:3b"
    assert settings.ollama_base_url == ""


def test_ollama_config_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:8b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11500")
    settings = Settings(_env_file=None)
    assert settings.ollama_model == "qwen3:8b"
    assert settings.ollama_base_url == "http://localhost:11500"


# ---------------------------------------------------------------------------
# #169 follow-up (ADR-002 E.18) — eval_provider, separate from adapter_provider
# ---------------------------------------------------------------------------


def test_eval_provider_defaults_to_ollama() -> None:
    settings = Settings(_env_file=None)
    assert settings.eval_provider == "ollama"


def test_eval_provider_overridable_via_env_independent_of_adapter_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The eval's provider choice must never be coupled to the running
    app's own adapter_provider -- flipping one must not flip the other."""
    monkeypatch.setenv("ADAPTER_PROVIDER", "groq")
    monkeypatch.setenv("EVAL_PROVIDER", "openai_compatible")
    settings = Settings(_env_file=None)
    assert settings.adapter_provider == "groq"
    assert settings.eval_provider == "openai_compatible"


# ---------------------------------------------------------------------------
# openai-compatible-provider — provider value + credential fields (spec R1, R2)
# ---------------------------------------------------------------------------

_OPENAI_COMPATIBLE_ENV = (
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
    "OPENAI_COMPATIBLE_MODEL",
)


def test_adapter_provider_accepts_openai_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter_provider accepts the new openai_compatible value (spec R1)."""
    monkeypatch.setenv("ADAPTER_PROVIDER", "openai_compatible")
    settings = Settings(_env_file=None)
    assert settings.adapter_provider == "openai_compatible"


@pytest.mark.parametrize("provider", ["ollama", "groq", "anthropic"])
def test_adapter_provider_still_accepts_existing_values(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Widening the Literal must not invalidate values that already worked."""
    monkeypatch.setenv("ADAPTER_PROVIDER", provider)
    assert Settings(_env_file=None).adapter_provider == provider


def test_openai_compatible_config_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three openai_compatible_* fields default to empty (spec R2).

    Empty defaults keep the app bootable without an env file; enforcing the
    required ones is _build_chat_model's job, not Settings' (design AD-6).
    """
    for var in _OPENAI_COMPATIBLE_ENV:
        monkeypatch.delenv(var, raising=False)
    settings = Settings(_env_file=None)
    assert settings.openai_compatible_api_key == ""
    assert settings.openai_compatible_base_url == ""
    assert settings.openai_compatible_model == ""


def test_openai_compatible_config_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "some-model")
    settings = Settings(_env_file=None)
    assert settings.openai_compatible_base_url == "https://example.test/v1"
    assert settings.openai_compatible_model == "some-model"


def test_openai_compatible_key_is_independent_from_embeddings_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai_api_key and openai_compatible_api_key are distinct fields (spec R2).

    Reusing openai_api_key (the embeddings credential) would make it impossible
    to run embeddings against OpenAI and chat against another host at once.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "embeddings-key")
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "chat-key")
    settings = Settings(_env_file=None)
    assert settings.openai_api_key == "embeddings-key"
    assert settings.openai_compatible_api_key == "chat-key"


# ---------------------------------------------------------------------------
# D-014 S4 — checkpointer/persistence config fields (design AD-7)
# ---------------------------------------------------------------------------


def test_whatsapp_checkpointer_config_defaults():
    """whatsapp_checkpointer_enabled defaults True, checkpointer_ttl_s defaults 86400s."""
    settings = Settings(_env_file=None)
    assert settings.whatsapp_checkpointer_enabled is True
    assert settings.checkpointer_ttl_s == 86400


def test_whatsapp_checkpointer_config_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_CHECKPOINTER_ENABLED", "false")
    monkeypatch.setenv("CHECKPOINTER_TTL_S", "3600")
    settings = Settings(_env_file=None)
    assert settings.whatsapp_checkpointer_enabled is False
    assert settings.checkpointer_ttl_s == 3600


def test_checkpointer_ttl_s_accepts_none() -> None:
    """checkpointer_ttl_s=None means no expiry (design AD-7 idle expiry is optional)."""
    settings = Settings(_env_file=None, checkpointer_ttl_s=None)
    assert settings.checkpointer_ttl_s is None


# ---------------------------------------------------------------------------
# D-023 — dedicated read-only BI connection
# ---------------------------------------------------------------------------


def test_bi_database_url_defaults_to_empty_and_is_env_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty means 'BI disabled', not 'fall back to the main engine'.

    The BI tool must never borrow the app's read-write engine — the read-only
    role is the guardrail that survives a bug in every layer above it.
    """
    assert Settings(_env_file=None).bi_database_url == ""

    monkeypatch.setenv("BI_DATABASE_URL", "postgresql+asyncpg://ro@localhost/x")
    assert Settings(_env_file=None).bi_database_url.endswith("/x")


# D-014 S5 — fail-CLOSED security guards (BLOCKER 1: no empty-secret boot)
# ---------------------------------------------------------------------------


def test_allow_insecure_defaults_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The insecure opt-in must default OFF (secure-by-default).

    The test suite sets ALLOW_INSECURE=true in conftest so unrelated tests can
    build empty-secret Settings; remove it here to observe the real default.
    """
    monkeypatch.delenv("ALLOW_INSECURE", raising=False)
    settings = Settings(
        _env_file=None,
        adapter_runtimes=[],
        meta_webhook_secret="s",
    )
    assert settings.allow_insecure is False


def test_settings_raises_when_adapter_runtimes_set_and_adapter_api_key_empty() -> None:
    """adapter_runtimes configured + empty adapter_api_key must fail closed."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            adapter_runtimes=["acme__sales-agent"],
            adapter_api_key="",
            meta_webhook_secret="s",  # isolate: only the adapter guard should fire
            allow_insecure=False,
        )
    assert "adapter_api_key" in str(excinfo.value)


def test_settings_boots_when_adapter_api_key_set() -> None:
    """Differential: the ONLY difference between boot and refusal is the key.

    Asserting ``settings.adapter_api_key == "k"`` would only echo the kwarg
    back — it survives deleting the validator outright. Pinning the pair
    instead makes the assertion about what the validator DECIDED.
    """
    common: dict[str, object] = {
        "_env_file": None,
        "adapter_runtimes": ["acme__sales-agent"],
        "meta_webhook_secret": "s",
        "allow_insecure": False,
    }

    with pytest.raises(ValidationError):
        Settings(adapter_api_key="", **common)  # type: ignore[arg-type]

    settings = Settings(adapter_api_key="k", **common)  # type: ignore[arg-type]
    assert settings.adapter_api_key == "k"


def test_settings_boots_when_adapter_runtimes_empty_and_key_empty() -> None:
    """Differential: an empty adapter key is only fatal WITH runtimes.

    The guard is ``adapter_runtimes and not adapter_api_key`` — this pins both
    halves of the conjunction, so dropping either one goes red.
    """
    common: dict[str, object] = {
        "_env_file": None,
        "adapter_api_key": "",
        "meta_webhook_secret": "s",
        "allow_insecure": False,
    }

    with pytest.raises(ValidationError):
        Settings(adapter_runtimes=["acme__sales-agent"], **common)  # type: ignore[arg-type]

    settings = Settings(adapter_runtimes=[], **common)  # type: ignore[arg-type]
    assert settings.adapter_runtimes == []


@pytest.mark.parametrize(
    ("insecure_kwargs", "guard"),
    [
        (
            {"adapter_runtimes": ["acme__sales-agent"], "adapter_api_key": ""},
            "adapter_api_key",
        ),
        ({"adapter_runtimes": [], "meta_webhook_secret": ""}, "meta_webhook_secret"),
    ],
    ids=["adapter-key-guard", "webhook-secret-guard"],
)
def test_allow_insecure_is_the_only_thing_that_bypasses_a_guard(
    insecure_kwargs: dict[str, object], guard: str
) -> None:
    """``allow_insecure`` must be the SOLE difference between refusal and boot.

    Run the identical insecure config twice, flipping only the flag. The False
    leg proves the guard fires (and names itself); the True leg proves the
    ``if self.allow_insecure: return self`` early-out is what lets it through.
    Deleting either the guard or the early-out turns one leg red.
    """
    base: dict[str, object] = {
        "_env_file": None,
        "adapter_runtimes": [],
        "adapter_api_key": "",
        "meta_webhook_secret": "s",
    }
    base.update(insecure_kwargs)

    with pytest.raises(ValidationError) as excinfo:
        Settings(allow_insecure=False, **base)  # type: ignore[arg-type]
    assert guard in str(excinfo.value)

    settings = Settings(allow_insecure=True, **base)  # type: ignore[arg-type]
    assert settings.allow_insecure is True


def test_settings_raises_when_meta_webhook_secret_empty() -> None:
    """Empty meta_webhook_secret makes HMAC forgeable — must fail closed."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            adapter_runtimes=[],  # isolate: only the meta guard should fire
            meta_webhook_secret="",
            allow_insecure=False,
        )
    assert "meta_webhook_secret" in str(excinfo.value)


def test_settings_boots_when_meta_webhook_secret_set() -> None:
    """Differential: only the emptiness of the secret decides boot vs refusal.

    (The ``allow_insecure=True`` bypass for this guard is covered by
    ``test_allow_insecure_is_the_only_thing_that_bypasses_a_guard``.)
    """
    common: dict[str, object] = {
        "_env_file": None,
        "adapter_runtimes": [],
        "allow_insecure": False,
    }

    with pytest.raises(ValidationError):
        Settings(meta_webhook_secret="", **common)  # type: ignore[arg-type]

    settings = Settings(meta_webhook_secret="s", **common)  # type: ignore[arg-type]
    assert settings.meta_webhook_secret == "s"


# ---------------------------------------------------------------------------
# D-014 S5 — gaps that BLOCKER 1 leaves open. These are xfail(strict=True):
# they describe behaviour the code does NOT have yet, so they report xfail
# today and turn the suite RED the moment someone implements the fix without
# removing the marker. They are executable tickets, not passing coverage.
# ---------------------------------------------------------------------------


def test_shipped_env_example_boots_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The onboarding path documented in the README must produce a bootable app.

    This was an `xfail(strict=True)` recording a real bug: `cp .env.example
    .env` died with a raw pydantic traceback at import, before
    `setup_logging()` ran. Emptying the `adapter_runtimes` default fixed it —
    with no runtime configured there is nothing to protect, so the
    fail-closed guard no longer fires on a file that ships no
    `ADAPTER_API_KEY`.

    The xfail reason this replaces was wrong on both counts: it claimed
    `.env.example` ships no `META_WEBHOOK_SECRET` (it does, with a value)
    and that Settings still dies at import (it does not). Corrected rather
    than left to rot, since a stale xfail reason is a lie that survives
    every green run.

    Every other test is immunised from this by conftest forcing
    ALLOW_INSECURE=true, so nothing else in the suite builds Settings from
    the shipped file with the shipped defaults.
    """
    for var in ("ALLOW_INSECURE", "ADAPTER_API_KEY", "META_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)

    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    assert env_example.is_file(), "the repo must ship an .env.example to copy"

    settings = Settings(_env_file=str(env_example))

    # It boots...
    assert settings.meta_webhook_secret, "the shipped file must carry a secret"
    # ...and it boots SAFELY: no runtime is exposed, which is why no adapter
    # key is needed. If a future edit adds one to the file, the fail-closed
    # guard fires again and this assertion says so before a user hits it.
    assert not settings.adapter_runtimes, (
        "the shipped example must not configure a runtime without a key"
    )


# ---------------------------------------------------------------------------
# Platform surface vs consumer extension
# ---------------------------------------------------------------------------


def test_configured_runtimes_without_a_key_still_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Emptying the default must not weaken the adapter guard.

    `adapter_runtimes` used to default to a deployment name, so the guard
    fired for a bare `Settings()`. It now fires when a runtime is actually
    configured, which is the case that matters: an exposed `/v1/*` runtime
    carries the role's full write grants.
    """
    # conftest exports ALLOW_INSECURE=true for the whole suite, which is
    # exactly what this test must not inherit.
    monkeypatch.delenv("ALLOW_INSECURE", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            adapter_runtimes=["acme__sales-agent"],
            meta_webhook_secret="not-the-point",
        )

    assert "adapter_api_key is required" in str(excinfo.value)


def test_settings_carries_no_deployment_name() -> None:
    """No platform default may name a deployment.

    A default like `db_name="acme"` or `whatsapp_runtime_id="acme__sales-agent"`
    ships one deployment's topology to every consumer of the library, and does
    it silently — the value works, it is just someone else's.
    """
    settings = Settings(_env_file=None, allow_insecure=True)

    assert "acme" not in settings.database_url
    assert settings.db_name == "agents_system"
    assert settings.whatsapp_runtime_id == ""
    assert settings.adapter_runtimes == []


def test_a_consumer_can_extend_settings_without_editing_the_library() -> None:
    """The extension point: a subclass adds fields and its own validator.

    This is what replaces the `medallion_*` block that used to sit in the
    platform's own `Settings`. Both the subclass's validator and the
    platform's still run.
    """
    from agents_system.services.medallion import MedallionSettings

    settings = MedallionSettings(
        _env_file=None,
        allow_insecure=True,
        db_user="app",
        db_password="p@ss/word",
        db_host="db.internal",
        db_name="acme",
        medallion_db_name="warehouse",
    )

    # The platform validator composed the main URL...
    assert "db.internal" in settings.database_url
    assert settings.database_url.endswith("/acme")
    # ...and the subclass's composed its own, falling back to the shared host,
    # with the password url-encoded rather than breaking the connection string.
    assert settings.medallion_database_url.endswith("/warehouse")
    assert "p%40ss%2Fword" in settings.medallion_database_url


# ---------------------------------------------------------------------------
# Restored: deleted by mistake while rewriting the env-example xfail
# ---------------------------------------------------------------------------
#
# This is a strict-xfail security tripwire and it records a gap that is
# STILL OPEN. It was removed in the same edit that converted the
# neighbouring xfail into a passing test, with no mention in the commit
# message -- the two were adjacent, and one rewrite took both. Deleting a
# failing security test is the loudest possible way to close a security
# gap without fixing it.


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SOURCE FIX REQUIRED: allow_insecure has no production tripwire. "
        "Settings.environment exists and is never cross-checked, so an operator "
        "who copies a dev .env (ALLOW_INSECURE=true) into ENVIRONMENT=production "
        "boots with /v1/* unauthenticated and webhook signatures forgeable."
    ),
)
@pytest.mark.parametrize("environment", ["production", "staging"])
def test_allow_insecure_is_rejected_outside_development(environment: str) -> None:
    """The insecure opt-out must not be usable in a non-development environment.

    BLOCKER 1 moved enforcement entirely to boot time but left both downstream
    fail-open branches intact: ``openai_adapter.verify_bearer`` still returns
    early on an empty key, and ``meta_signature.verify_signature`` still
    computes HMAC with an empty secret. ``allow_insecure`` is therefore the only
    thing standing between a mis-copied .env and a fully open production app.
    """
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            environment=environment,
            allow_insecure=True,
            adapter_runtimes=["acme__sales-agent"],
            adapter_api_key="",
            meta_webhook_secret="",
        )
    assert "allow_insecure" in str(excinfo.value)
