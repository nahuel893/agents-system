"""Tests for agentsys.config — Settings loading and singleton."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from agentsys.config import Settings, get_settings


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


def test_get_settings_returns_singleton():
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


# ---------------------------------------------------------------------------
# D-012 — adapter config fields
# ---------------------------------------------------------------------------


def test_adapter_config_defaults():
    """adapter_api_key, adapter_provider, and adapter_runtimes have correct defaults."""
    settings = Settings(_env_file=None)
    assert settings.adapter_api_key == ""
    assert settings.adapter_provider == "ollama"
    assert settings.adapter_runtimes == ["badie__sales-agent"]


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
            adapter_runtimes=["badie__sales-agent"],
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
    common: dict[str, object] = dict(
        _env_file=None,
        adapter_runtimes=["badie__sales-agent"],
        meta_webhook_secret="s",
        allow_insecure=False,
    )

    with pytest.raises(ValidationError):
        Settings(adapter_api_key="", **common)  # type: ignore[arg-type]

    settings = Settings(adapter_api_key="k", **common)  # type: ignore[arg-type]
    assert settings.adapter_api_key == "k"


def test_settings_boots_when_adapter_runtimes_empty_and_key_empty() -> None:
    """Differential: an empty adapter key is only fatal WITH runtimes.

    The guard is ``adapter_runtimes and not adapter_api_key`` — this pins both
    halves of the conjunction, so dropping either one goes red.
    """
    common: dict[str, object] = dict(
        _env_file=None,
        adapter_api_key="",
        meta_webhook_secret="s",
        allow_insecure=False,
    )

    with pytest.raises(ValidationError):
        Settings(adapter_runtimes=["badie__sales-agent"], **common)  # type: ignore[arg-type]

    settings = Settings(adapter_runtimes=[], **common)  # type: ignore[arg-type]
    assert settings.adapter_runtimes == []


@pytest.mark.parametrize(
    ("insecure_kwargs", "guard"),
    [
        (
            dict(adapter_runtimes=["badie__sales-agent"], adapter_api_key=""),
            "adapter_api_key",
        ),
        (dict(adapter_runtimes=[], meta_webhook_secret=""), "meta_webhook_secret"),
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
    base: dict[str, object] = dict(
        _env_file=None,
        adapter_runtimes=[],
        adapter_api_key="",
        meta_webhook_secret="s",
    )
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
    common: dict[str, object] = dict(
        _env_file=None,
        adapter_runtimes=[],
        allow_insecure=False,
    )

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
            adapter_runtimes=["badie__sales-agent"],
            adapter_api_key="",
            meta_webhook_secret="",
        )
    assert "allow_insecure" in str(excinfo.value)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "SOURCE FIX REQUIRED: .env.example ships neither ADAPTER_API_KEY nor "
        "ALLOW_INSECURE, and adapter_runtimes defaults to a non-empty list, so "
        "the documented `cp .env.example .env` first-run path now dies with a "
        "raw pydantic traceback at import — before setup_logging() runs."
    ),
)
def test_shipped_env_example_boots_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """The onboarding path documented in the README must produce a bootable app.

    Every other test is immunised from this by conftest forcing
    ALLOW_INSECURE=true, so nothing else in the suite ever builds Settings from
    the shipped env file with the shipped defaults.
    """
    for var in ("ALLOW_INSECURE", "ADAPTER_API_KEY", "META_WEBHOOK_SECRET"):
        monkeypatch.delenv(var, raising=False)

    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    assert env_example.is_file(), "the repo must ship an .env.example to copy"

    settings = Settings(_env_file=str(env_example))
    assert settings.adapter_api_key or settings.allow_insecure
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
