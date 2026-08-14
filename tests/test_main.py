"""Tests for the FastAPI app factory's lifespan (D-014 slices S1 and S4).

Covers:
  - data-driven grants (design AD-5): lifespan calls harness.loader.resolve()
    and passes definition.permissions as granted_permissions to build_runtime,
    instead of a hardcoded role -> permissions map.
  - outbound WhatsApp client (design AD-2): lifespan builds a WhatsAppClient
    and stores it on app.state.whatsapp_client.
  - checkpointer wiring (design AD-1/AD-7): lifespan builds the shared Redis
    checkpointer (unless whatsapp_checkpointer_enabled=False) and injects it
    into every AgentRuntime.
  - resource teardown robustness (S1/S2/S3 gate carry-forward): every
    teardown callback still runs even if an earlier one raises.

No real network / DB / embedder / LLM calls: every heavy dependency the
lifespan touches is patched at its defining module (main.py imports them
lazily inside the function body).
"""
from __future__ import annotations

from contextlib import ExitStack, asynccontextmanager
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentsys.agent.reasoning import ReasoningSanitizedChatOpenAI
from agentsys.config import Settings, get_settings
from agentsys.main import _build_chat_model, create_app, lifespan


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = dict(
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
        adapter_runtimes=["badie__sales-agent"],
        whatsapp_token="test-token",
        whatsapp_phone_number_id="1234567890",
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def _stack(patchers: tuple[Any, ...]) -> ExitStack:
    """Enter a tuple of context managers under a single ExitStack."""
    stack = ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


def _fake_checkpointer_cm_factory(
    fake_checkpointer: Any, aexit_calls: list[str] | None = None
):
    """Build a callable matching `_build_checkpointer_cm(settings)`'s
    signature/return shape: an async context manager yielding a fake
    checkpointer, with no real Redis connection involved."""

    @asynccontextmanager
    async def _cm(*args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        try:
            yield fake_checkpointer
        finally:
            if aexit_calls is not None:
                aexit_calls.append("checkpointer_exit")

    return _cm


@pytest.mark.asyncio
async def test_lifespan_uses_data_driven_grants() -> None:
    """lifespan resolves the definition FIRST and passes its permissions to
    build_runtime as granted_permissions — not a hardcoded role map."""
    test_settings = _make_settings()

    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog", "write:orders")
    fake_definition.execution_limits = None

    fake_equipped = MagicMock()
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agentsys.main.get_settings", return_value=test_settings),
        patch("agentsys.main.get_engine", return_value=mock_engine),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
        patch("agentsys.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agentsys.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(MagicMock()),
        ),
        patch(
            "agentsys.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.connectors.rag_connector.build_badie_rag_registry",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.harness.loader.resolve", return_value=fake_definition
        ) as mock_resolve,
        patch(
            "agentsys.harness.factory.build_runtime", return_value=fake_equipped
        ) as mock_build_runtime,
        patch("agentsys.agent.graph.AgentRuntime", return_value=MagicMock()),
    ):
        app = create_app()

        async with lifespan(app):
            assert app.state.runtimes

        # resolve() called for the sales-agent role with the badie deployment
        mock_resolve.assert_any_call("sales-agent", client="badie")

        # build_runtime received the resolved definition's permissions —
        # NOT a hardcoded role -> permissions map.
        _, kwargs = mock_build_runtime.call_args
        assert tuple(kwargs["granted_permissions"]) == ("read:catalog", "write:orders")


@pytest.mark.asyncio
async def test_lifespan_builds_whatsapp_client() -> None:
    """lifespan builds a WhatsAppClient and stores it on app.state."""
    test_settings = _make_settings(adapter_runtimes=[])
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agentsys.main.get_settings", return_value=test_settings),
        patch("agentsys.main.get_engine", return_value=mock_engine),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
    ):
        app = create_app()

        async with lifespan(app):
            from agentsys.integration.whatsapp_client import WhatsAppClient

            assert isinstance(app.state.whatsapp_client, WhatsAppClient)


# ---------------------------------------------------------------------------
# D-014 S4 — checkpointer wiring (design AD-1/AD-7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_injects_checkpointer_into_runtimes() -> None:
    """whatsapp_checkpointer_enabled=True (default) — lifespan builds the
    checkpointer and passes it to every AgentRuntime constructor call."""
    test_settings = _make_settings()
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = None
    fake_equipped = MagicMock()
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    fake_checkpointer = MagicMock(name="fake_checkpointer")

    with (
        patch("agentsys.main.get_settings", return_value=test_settings),
        patch("agentsys.main.get_engine", return_value=mock_engine),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
        patch("agentsys.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agentsys.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(fake_checkpointer),
        ) as mock_build_checkpointer_cm,
        patch(
            "agentsys.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.connectors.rag_connector.build_badie_rag_registry",
            return_value=MagicMock(),
        ),
        patch("agentsys.harness.loader.resolve", return_value=fake_definition),
        patch("agentsys.harness.factory.build_runtime", return_value=fake_equipped),
        patch("agentsys.agent.graph.AgentRuntime") as mock_agent_runtime,
    ):
        app = create_app()

        async with lifespan(app):
            assert app.state.runtimes

        mock_build_checkpointer_cm.assert_called_once()
        _, kwargs = mock_agent_runtime.call_args
        assert kwargs["checkpointer"] is fake_checkpointer


@pytest.mark.asyncio
async def test_lifespan_skips_checkpointer_when_disabled() -> None:
    """whatsapp_checkpointer_enabled=False — lifespan never builds the
    checkpointer; every AgentRuntime gets checkpointer=None."""
    test_settings = _make_settings(whatsapp_checkpointer_enabled=False)
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = None
    fake_equipped = MagicMock()
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agentsys.main.get_settings", return_value=test_settings),
        patch("agentsys.main.get_engine", return_value=mock_engine),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
        patch("agentsys.main._build_chat_model", return_value=MagicMock()),
        patch("agentsys.main._build_checkpointer_cm") as mock_build_checkpointer_cm,
        patch(
            "agentsys.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.connectors.rag_connector.build_badie_rag_registry",
            return_value=MagicMock(),
        ),
        patch("agentsys.harness.loader.resolve", return_value=fake_definition),
        patch("agentsys.harness.factory.build_runtime", return_value=fake_equipped),
        patch("agentsys.agent.graph.AgentRuntime") as mock_agent_runtime,
    ):
        app = create_app()

        async with lifespan(app):
            assert app.state.runtimes

        mock_build_checkpointer_cm.assert_not_called()
        _, kwargs = mock_agent_runtime.call_args
        assert kwargs["checkpointer"] is None


@pytest.mark.asyncio
async def test_lifespan_resource_teardown_survives_engine_dispose_failure() -> None:
    """engine.dispose() raising must not prevent whatsapp_client.aclose() or
    the checkpointer context's exit from running (S1/S2/S3 gate
    carry-forward: teardown must be robust to a single resource's failure)."""
    test_settings = _make_settings()
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = None
    fake_equipped = MagicMock()

    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock(side_effect=RuntimeError("dispose boom"))

    aexit_calls: list[str] = []
    fake_checkpointer = MagicMock(name="fake_checkpointer")
    mock_aclose = AsyncMock()

    with (
        patch("agentsys.main.get_settings", return_value=test_settings),
        patch("agentsys.main.get_engine", return_value=mock_engine),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
        patch("agentsys.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agentsys.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(fake_checkpointer, aexit_calls),
        ),
        patch(
            "agentsys.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.connectors.rag_connector.build_badie_rag_registry",
            return_value=MagicMock(),
        ),
        patch("agentsys.harness.loader.resolve", return_value=fake_definition),
        patch("agentsys.harness.factory.build_runtime", return_value=fake_equipped),
        patch("agentsys.agent.graph.AgentRuntime", return_value=MagicMock()),
        patch(
            "agentsys.integration.whatsapp_client.WhatsAppClient.aclose",
            new=mock_aclose,
        ),
    ):
        app = create_app()

        with pytest.raises(RuntimeError, match="dispose boom"):
            async with lifespan(app):
                pass

        mock_aclose.assert_awaited_once()
        assert "checkpointer_exit" in aexit_calls


# ---------------------------------------------------------------------------
# openai-compatible-provider — _build_chat_model dispatch (spec R3, R6, R7)
# ---------------------------------------------------------------------------


def _openai_compatible_settings(**overrides: object) -> Settings:
    values: dict[str, object] = dict(
        openai_compatible_base_url="https://example.test/v1",
        openai_compatible_model="test-model",
        openai_compatible_api_key="test-key",
    )
    values.update(overrides)
    return _make_settings(**values)


@pytest.mark.parametrize(
    ("provider", "expected_class"),
    [
        ("ollama", "ChatOllama"),
        ("groq", "ChatGroq"),
        ("anthropic", "ChatAnthropic"),
        ("openai_compatible", "ReasoningSanitizedChatOpenAI"),
    ],
)
def test_build_chat_model_dispatches_by_provider(
    provider: str, expected_class: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each provider value builds its own model type; existing ones still work.

    GROQ_API_KEY / ANTHROPIC_API_KEY are faked because those SDK constructors
    validate eagerly and raise on a None key. Without that, this test would
    fail for environment reasons on a clean machine instead of testing
    dispatch — an environment crash wearing a red test's clothes.

    The openai_compatible branch is deliberately left UNMOCKED so its wiring
    is genuinely exercised.
    """
    monkeypatch.setenv("GROQ_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    with patch(
        "agentsys.main.get_settings", return_value=_openai_compatible_settings()
    ):
        model = _build_chat_model(provider)

    assert type(model).__name__ == expected_class


def test_build_chat_model_wires_openai_compatible_from_settings() -> None:
    with patch(
        "agentsys.main.get_settings", return_value=_openai_compatible_settings()
    ):
        model = _build_chat_model("openai_compatible")

    assert model.model_name == "test-model"
    assert model.openai_api_base == "https://example.test/v1"
    assert model.openai_api_key.get_secret_value() == "test-key"


@pytest.mark.parametrize(
    ("missing_field", "expected_env_var"),
    [
        ("openai_compatible_base_url", "OPENAI_COMPATIBLE_BASE_URL"),
        ("openai_compatible_model", "OPENAI_COMPATIBLE_MODEL"),
    ],
)
def test_build_chat_model_requires_base_url_and_model(
    missing_field: str, expected_env_var: str
) -> None:
    """Missing required config fails loudly, naming the env var.

    Without the base_url guard, ChatOpenAI would silently target OpenAI's own
    API — wrong vendor, wrong credential, opaque auth error later.
    """
    settings = _openai_compatible_settings(**{missing_field: ""})
    with patch("agentsys.main.get_settings", return_value=settings):
        with pytest.raises(ValueError, match=expected_env_var) as excinfo:
            _build_chat_model("openai_compatible")

    # The message names the variable, never a credential value.
    assert "test-key" not in str(excinfo.value)


def test_build_chat_model_accepts_empty_api_key_for_keyless_hosts() -> None:
    """An empty API key must not break the provider.

    ChatOpenAI raises openai.OpenAIError when api_key is None or "", but
    keyless OpenAI-compatible hosts (self-hosted vLLM, LM Studio, llama.cpp)
    are legitimate. The branch substitutes a placeholder and warns, so the
    provider stays usable and a forgotten key is still visible at startup.
    """
    fake_logger = MagicMock()
    with (
        patch(
            "agentsys.main.get_settings",
            return_value=_openai_compatible_settings(openai_compatible_api_key=""),
        ),
        patch("agentsys.main.structlog.get_logger", return_value=fake_logger),
    ):
        model = _build_chat_model("openai_compatible")

    assert model.openai_api_key.get_secret_value() != ""
    warned = [c.args[0] for c in fake_logger.warning.call_args_list if c.args]
    assert "openai_compatible.no_api_key" in warned


def test_openai_compatible_model_still_supports_bind_tools() -> None:
    """graph.py binds tools to whatever the factory returns (graph.py:374).

    If the binding stopped wrapping our subclass, sanitization would silently
    stop applying the moment the agent equips a tool.
    """
    with patch(
        "agentsys.main.get_settings", return_value=_openai_compatible_settings()
    ):
        model = _build_chat_model("openai_compatible")

    bound = model.bind_tools(
        [
            {
                "name": "catalog_search",
                "description": "Search the catalog.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ]
    )
    assert isinstance(bound.bound, ReasoningSanitizedChatOpenAI)


# ---------------------------------------------------------------------------
# D-014 S5 — dedup-TTL invariant (BLOCKER 3: total_execution_timeout_s must be
# < DEDUP_TTL_SECONDS or a slow turn can outlive the dedup key and Meta's retry
# triggers a second full processing + double send).
# ---------------------------------------------------------------------------


def _dedup_invariant_patches(fake_definition: Any):
    """Common lifespan patches for the dedup-TTL invariant tests. Checkpointer
    is disabled so no real Redis connection is attempted."""
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    return mock_engine, (
        patch("agentsys.main.get_engine", return_value=mock_engine),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
        patch("agentsys.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agentsys.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.connectors.rag_connector.build_badie_rag_registry",
            return_value=MagicMock(),
        ),
        patch("agentsys.harness.loader.resolve", return_value=fake_definition),
        patch("agentsys.harness.factory.build_runtime", return_value=MagicMock()),
        patch("agentsys.agent.graph.AgentRuntime", return_value=MagicMock()),
    )


@pytest.mark.parametrize("timeout_s", [300, 301])
@pytest.mark.asyncio
async def test_lifespan_rejects_runtime_when_total_timeout_ge_dedup_ttl(
    timeout_s: int,
) -> None:
    """A runtime whose effective total_execution_timeout_s >= DEDUP_TTL_SECONDS
    (300) must make the app refuse to boot, with both values in the message."""
    from agentsys.services.dedup import DEDUP_TTL_SECONDS

    test_settings = _make_settings(whatsapp_checkpointer_enabled=False)
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = {"total_execution_timeout_s": timeout_s}

    _, patches = _dedup_invariant_patches(fake_definition)

    with patch("agentsys.main.get_settings", return_value=test_settings):
        with _stack(patches):
            app = create_app()
            with pytest.raises(
                (ValueError, RuntimeError)
            ) as excinfo:
                async with lifespan(app):
                    pass

    message = str(excinfo.value)
    assert str(timeout_s) in message
    assert str(DEDUP_TTL_SECONDS) in message


@pytest.mark.asyncio
async def test_lifespan_accepts_runtime_just_under_dedup_ttl() -> None:
    """Boundary: total_execution_timeout_s = 299 (< 300) boots cleanly.

    This pins the guard's comparison as ``>=`` (strictly-less passes), i.e. it
    is a negative control against a guard that rejects everything. It does NOT
    certify 299 as an operationally safe budget: the dedup key's 300s TTL starts
    BEFORE the client lookup and the reply is sent AFTER the conversation-log
    write, so ``total_execution_timeout_s`` (which bounds only the graph invoke)
    has no headroom for the surrounding work at that value. Giving the
    invariant real headroom is a source change — see the module docstring note.
    """
    test_settings = _make_settings(whatsapp_checkpointer_enabled=False)
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = {"total_execution_timeout_s": 299}

    _, patches = _dedup_invariant_patches(fake_definition)

    with patch("agentsys.main.get_settings", return_value=test_settings):
        with _stack(patches):
            app = create_app()
            async with lifespan(app):
                assert app.state.runtimes


@pytest.mark.asyncio
async def test_lifespan_accepts_default_limits_invariant_holds() -> None:
    """Platform default (60 < 300) boots cleanly when no override is set."""
    test_settings = _make_settings(whatsapp_checkpointer_enabled=False)
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = None

    _, patches = _dedup_invariant_patches(fake_definition)

    with patch("agentsys.main.get_settings", return_value=test_settings):
        with _stack(patches):
            app = create_app()
            async with lifespan(app):
                assert app.state.runtimes


@pytest.mark.parametrize(
    ("execution_limits", "case"),
    [
        ({"max_tool_calls": 5}, "partial-override-without-timeout-key"),
        ({"total_execution_timeout_s": None}, "explicit-none-timeout"),
    ],
)
@pytest.mark.asyncio
async def test_lifespan_guard_reads_the_merged_effective_limits(
    execution_limits: dict[str, Any], case: str
) -> None:
    """The guard must go through ``_effective_limits``, not index the raw dict.

    Design AD-3 lets a role override execution_limits PARTIALLY: any key it
    omits (or sets to None) falls back to the platform default. A guard that
    read ``definition.execution_limits["total_execution_timeout_s"]`` directly
    would raise KeyError/TypeError on exactly these shapes and take the whole
    app down at startup for a perfectly legal role config.
    """
    test_settings = _make_settings(whatsapp_checkpointer_enabled=False)
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = execution_limits

    _, patches = _dedup_invariant_patches(fake_definition)

    with patch("agentsys.main.get_settings", return_value=test_settings):
        with _stack(patches):
            app = create_app()
            async with lifespan(app):
                assert app.state.runtimes


def _load_main_as_fresh_module(module_name: str) -> Any:
    """Execute ``src/agentsys/main.py`` again under a throwaway module name.

    ``importlib.reload`` would rebind the real ``agentsys.main`` (and rebuild
    its module-level ``app``) for every test that ran before or after this one.
    A fresh spec keeps ``sys.modules['agentsys.main']`` untouched while still
    executing every module-level statement — which is the only way to observe
    an import-time ``assert``.
    """
    import importlib.util

    import agentsys.main as real_main

    spec = importlib.util.spec_from_file_location(module_name, real_main.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("violating_default", [300, 301, 600])
def test_import_time_guard_rejects_a_violating_platform_default(
    monkeypatch: pytest.MonkeyPatch, violating_default: int
) -> None:
    """main.py must refuse to IMPORT when the shipped platform default itself
    violates the dedup-TTL coupling.

    The predecessor of this test re-asserted ``PLATFORM_DEFAULT_LIMITS[...] <
    DEDUP_TTL_SECONDS`` in its own body and never loaded the module whose guard
    it named — it passed with the production ``assert`` deleted. This one
    re-executes main.py with the platform default poisoned, so the assert is
    the only thing that can produce the expected failure.
    """
    from agentsys.harness.loader import PLATFORM_DEFAULT_LIMITS

    monkeypatch.setitem(
        PLATFORM_DEFAULT_LIMITS, "total_execution_timeout_s", violating_default
    )

    with pytest.raises(AssertionError) as excinfo:
        _load_main_as_fresh_module(f"_main_guard_probe_{violating_default}")

    message = str(excinfo.value)
    assert "total_execution_timeout_s" in message
    assert str(violating_default) in message
    assert "double-send" in message


def test_shipped_platform_default_satisfies_dedup_ttl_invariant() -> None:
    """The shipped platform default must satisfy the coupling on its own.

    Deliberately duplicated with the import-time ``assert``: ``assert``
    statements are stripped under ``python -O`` / ``PYTHONOPTIMIZE=1``, so in an
    optimised interpreter this test is the ONLY thing left checking the shipped
    default. It also gives a named failure instead of a collection-time
    explosion in every module that imports ``agentsys.main``.
    """
    from agentsys.harness.loader import PLATFORM_DEFAULT_LIMITS
    from agentsys.services.dedup import DEDUP_TTL_SECONDS

    assert PLATFORM_DEFAULT_LIMITS["total_execution_timeout_s"] < DEDUP_TTL_SECONDS


# ---------------------------------------------------------------------------
# D-023 — the BI engine and the read-only guarantee it rests on
#
# The BI block lives inside `if settings.adapter_runtimes:`, so these tests
# supply a runtime and patch the heavy dependencies the surrounding branch
# pulls in. A first draft passed `adapter_runtimes=[]`, never reached the
# block at all, and one assertion passed against unwritten code.
# ---------------------------------------------------------------------------


def _bi_settings(**overrides: object) -> Settings:
    values: dict[str, object] = dict(
        adapter_runtimes=["badie__sales-agent"],
        whatsapp_checkpointer_enabled=False,
        bi_database_url="postgresql+asyncpg://bi_readonly:pw@localhost:5432/badie",
    )
    values.update(overrides)
    return _make_settings(**values)


def _fake_bi_engine(value: str | None, *, raises: Exception | None = None) -> Any:
    """Fake engine whose `SHOW default_transaction_read_only` answers *value*."""

    class _Result:
        def scalar(self) -> Any:
            return value

    class _Conn:
        async def execute(self, *_: Any, **__: Any) -> Any:
            if raises is not None:
                raise raises
            return _Result()

        async def __aenter__(self) -> "_Conn":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    engine = MagicMock()
    engine.connect = lambda: _Conn()
    engine.dispose = AsyncMock()
    return engine


def _bi_lifespan_patches(settings: Settings, bi_engine: Any) -> tuple[Any, ...]:
    app_engine = MagicMock()
    app_engine.dispose = AsyncMock()
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog", "read:reports")
    fake_definition.execution_limits = None

    return (
        patch("agentsys.main.get_settings", return_value=settings),
        patch(
            "agentsys.main.get_engine",
            side_effect=lambda url: bi_engine
            if url == settings.bi_database_url
            else app_engine,
        ),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
        patch("agentsys.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agentsys.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.connectors.rag_connector.build_badie_rag_registry",
            return_value=MagicMock(),
        ),
        patch("agentsys.harness.loader.resolve", return_value=fake_definition),
        patch("agentsys.harness.factory.build_runtime", return_value=MagicMock()),
        patch("agentsys.agent.graph.AgentRuntime", return_value=MagicMock()),
    )


def _run_lifespan_capturing_bi_engine(settings: Settings, bi_engine: Any) -> Any:
    """Run lifespan and return the `bi_engine` the registry builder received.

    The decision this asserts is BINDING, not registering. `run_report` is now
    always registered — a tool a manifest names but the registry lacks makes
    the whole role unbuildable — so "is it usable" is exactly "was an engine
    bound", which is the argument captured here.
    """
    captured: dict[str, Any] = {}

    def fake_build_registry(_settings: Any, _embedder: Any, engine: Any = None) -> Any:
        captured["bi_engine"] = engine
        return MagicMock()

    async def _run() -> Any:
        with _stack(_bi_lifespan_patches(settings, bi_engine)):
            with patch(
                "agentsys.connectors.rag_connector.build_badie_rag_registry",
                side_effect=fake_build_registry,
            ):
                app = create_app()
                async with lifespan(app):
                    pass
        # NOT captured.get(): a builder that was never called would return
        # None, which is indistinguishable from the fail-closed result the
        # "role can write" test asserts. Missing means the patch target is
        # wrong, and that must be a failure, not a pass.
        assert "bi_engine" in captured, "build_badie_rag_registry was never called"
        return captured["bi_engine"]

    return _run()


@pytest.mark.asyncio
async def test_bi_engine_is_built_through_get_engine() -> None:
    """The BI engine must get `pool_pre_ping`, like every other engine here.

    `get_engine` sets `pool_pre_ping=True`; a bare `create_async_engine` does
    not. The BI engine is the one most likely to sit behind a connection its
    pool has held idle — a separate, possibly remote, read-only replica — so
    it is the worst one to leave without stale-connection detection.
    """
    settings = _bi_settings()
    bi_engine = _fake_bi_engine("on")
    patches = _bi_lifespan_patches(settings, bi_engine)

    with patches[0], patches[1] as mock_get:
        with _stack(patches[2:]):
            app = create_app()
            async with lifespan(app):
                pass

    assert settings.bi_database_url in [c.args[0] for c in mock_get.call_args_list]


@pytest.mark.asyncio
async def test_bi_tool_is_left_unbound_when_the_role_can_write() -> None:
    """Fail CLOSED when the database says the role is not read-only.

    The dedicated read-only role is the guardrail meant to hold even if
    validation and the interceptor both have bugs. Nothing verified it — the
    URL was simply trusted to point at a role someone configured by hand. If
    `default_transaction_read_only` is off the guardrail is absent, so no
    engine is bound and every call reports reporting unavailable.
    """
    bound = await _run_lifespan_capturing_bi_engine(
        _bi_settings(), _fake_bi_engine("off")
    )
    assert bound is None


@pytest.mark.asyncio
async def test_bi_tool_is_still_bound_when_the_check_cannot_run() -> None:
    """An unreachable reporting database must not become a boot failure.

    "Could not determine" is not "determined to be writable". Coupling startup
    to the reporting replica being up would take the whole sales bot down for
    a BI dependency; the tool degrades at call time into a structured error,
    which this slice already built.
    """
    from sqlalchemy.exc import OperationalError

    engine = _fake_bi_engine(
        None, raises=OperationalError("SHOW", {}, Exception("refused"))
    )
    bound = await _run_lifespan_capturing_bi_engine(_bi_settings(), engine)
    assert bound is engine


@pytest.mark.asyncio
async def test_bi_tool_is_unbound_when_no_url_is_configured() -> None:
    """No URL is the ordinary case, and it must not raise either."""
    bound = await _run_lifespan_capturing_bi_engine(
        _bi_settings(bi_database_url=""), _fake_bi_engine("on")
    )
    assert bound is None
