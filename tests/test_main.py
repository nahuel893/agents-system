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
    """Boundary: total_execution_timeout_s = 299 (< 300) boots cleanly."""
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


def test_module_import_guard_holds_for_platform_default() -> None:
    """Import-time backstop: the shipped platform default must satisfy the
    dedup-TTL coupling so a stock config can never double-send."""
    from agentsys.harness.loader import PLATFORM_DEFAULT_LIMITS
    from agentsys.services.dedup import DEDUP_TTL_SECONDS

    assert PLATFORM_DEFAULT_LIMITS["total_execution_timeout_s"] < DEDUP_TTL_SECONDS
