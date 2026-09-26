"""Tests for the FastAPI app factory's lifespan (D-014 slices S1 and S4).

Covers:
  - explicit deploy grants (permission-model PR3, issue #38, design.md
    Resolved Decision 5): lifespan calls harness.loader.resolve() for
    role/untrusted_input/limits facts, then looks up the GRANT itself in
    settings.deploy_grants (DEPLOY_GRANTS) — never definition.permissions,
    the role's full declared set. AD-5's auto-grant is gone; a configured
    runtime with no matching DEPLOY_GRANTS entry fails boot loudly.
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

import pathlib
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from datetime import timedelta
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import create_test_app

import agents_system.harness.factory  # noqa: F401 -- see the note below
from agents_system.agent.reasoning import ReasoningSanitizedChatOpenAI
from agents_system.config import Settings, get_settings
from agents_system.harness.loader import DefinitionError, RootConfig
from agents_system.main import _build_chat_model, lifespan
from agents_system.permissions import UntrustedInputGrantError

# `harness.factory` binds `resolve` from `harness.loader` at import time.
# Many tests below patch `agents_system.harness.loader.resolve`; if one of
# them were the first to import the factory, the factory would keep that
# mock for the rest of the session and every later test that builds a REAL
# runtime would get a MagicMock definition. Importing it here, unpatched,
# rules that out whatever order the tests run in.


@pytest.fixture(autouse=True)
def clear_settings_cache() -> Any:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ADR-004 PR4b -- the runtime ids the Settings-driven tests below name in
# ADAPTER_RUNTIMES/WHATSAPP_RUNTIME_ID, and the AGENT_REGISTRATIONS value
# each is registered with. `_make_settings` registers exactly the ids a test
# names; an id missing here (e.g. "not-registered") stays unregistered.
_ENV_REGISTRATIONS = {
    "sales": "sales-agent",
    "operator": "operator-agent",
}


def _default_deploy_grants(registrations: dict[str, str]) -> dict[str, tuple[str, ...]]:
    """Best-effort real-permission grant for every registered id, so tests
    using `_make_settings` that are not ABOUT deploy grants keep booting
    exactly as before permission-model PR3 (issue #38) removed AD-5's
    auto-grant-of-the-role's-full-permission-set. Resolves each id's role
    against the REAL platform/deployment roots (the same default `main.py`
    itself uses when a test passes no explicit `roots`) and grants exactly
    that role's own declared permissions -- equivalent to the removed
    auto-grant, for every test that never mocked `harness.loader.resolve`.
    A role that fails to resolve here (e.g. one whose test supplies its own
    non-default `roots=` to `create_test_app`) is simply skipped: that
    test's own earlier boot-time check fires before DEPLOY_GRANTS would be
    consulted regardless.
    """
    from agents_system.harness.loader import resolve as _resolve
    from agents_system.main import _parse_agent_registration

    grants: dict[str, tuple[str, ...]] = {}
    for runtime_id, value in registrations.items():
        role, client = _parse_agent_registration(value)
        try:
            definition = _resolve(role, client=client)
        except Exception:  # noqa: S112 -- best-effort test scaffolding, see docstring
            continue
        grants[runtime_id] = definition.permissions
    return grants


def _make_settings(**overrides: object) -> Settings:
    # #141 review follow-up -- whatsapp_token/whatsapp_phone_number_id are
    # deliberately NOT defaulted to non-empty here: main.py's lifespan now
    # fails closed at boot when both are configured but whatsapp_runtime_id
    # is empty, and the platform default whatsapp_runtime_id ("") is
    # exactly that. Most tests using this helper are not about WhatsApp at
    # all; a test that needs WhatsApp actually configured sets its own
    # whatsapp_token/whatsapp_phone_number_id alongside a registered
    # whatsapp_runtime_id.
    defaults: dict[str, object] = {
        "database_url": "postgresql+asyncpg://localhost:5432/agentsys_test",
        "redis_url": "redis://localhost:6379/0",
        "adapter_runtimes": ["sales"],
    }
    defaults.update(overrides)

    # ADR-004 PR4b -- the Settings-driven boot builds what
    # AGENT_REGISTRATIONS registers, and every channel id must be one of
    # those. Register the ids this test names, unless it passes its own.
    named: set[str] = set(defaults.get("adapter_runtimes") or [])  # type: ignore[arg-type]
    whatsapp_id = defaults.get("whatsapp_runtime_id")
    if whatsapp_id:
        named.add(whatsapp_id)  # type: ignore[arg-type]
    if "agent_registrations" not in overrides:
        defaults["agent_registrations"] = {
            runtime_id: _ENV_REGISTRATIONS[runtime_id]
            for runtime_id in named
            if runtime_id in _ENV_REGISTRATIONS
        }

    # permission-model PR3 (issue #38) -- boot now requires an explicit
    # DEPLOY_GRANTS entry per registered runtime id. A test that IS about
    # deploy grants passes its own `deploy_grants=` override, which wins.
    if "deploy_grants" not in overrides:
        defaults["deploy_grants"] = _default_deploy_grants(
            defaults["agent_registrations"]  # type: ignore[arg-type]
        )

    return Settings(**defaults)  # type: ignore[arg-type]


def _stack(patchers: tuple[Any, ...]) -> ExitStack:
    """Enter a tuple of context managers under a single ExitStack."""
    stack = ExitStack()
    for p in patchers:
        stack.enter_context(p)
    return stack


def _fake_checkpointer_cm_factory(
    fake_checkpointer: Any, aexit_calls: list[str] | None = None
) -> Any:
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
async def test_lifespan_uses_explicit_deploy_grants() -> None:
    """permission-model PR3 (issue #38, design.md Resolved Decision 5):
    lifespan resolves the definition to get role/untrusted_input/limits
    facts, but the GRANT itself comes only from settings.deploy_grants
    (DEPLOY_GRANTS) — never from definition.permissions (AD-5's auto-grant
    is gone)."""
    test_settings = _make_settings(
        deploy_grants={"sales": ("read:catalog", "write:orders")}
    )

    fake_definition = MagicMock()
    # Deliberately WIDER than (and different from) the configured grant, so
    # a passing assertion below cannot be explained by build_runtime having
    # received definition.permissions by coincidence.
    fake_definition.permissions = ("read:catalog", "write:orders", "send:message")
    fake_definition.execution_limits = None

    fake_equipped = MagicMock()
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(MagicMock()),
        ),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agents_system.harness.loader.resolve", return_value=fake_definition
        ) as mock_resolve,
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ) as mock_build_runtime,
        patch("agents_system.agent.graph.AgentRuntime", return_value=MagicMock()),
    ):
        app = create_test_app()

        async with lifespan(app):
            assert app.state.runtimes

        # resolve() called for the sales-agent role
        resolve_call = next(
            c for c in mock_resolve.call_args_list if c.args == ("sales-agent",)
        )
        assert resolve_call.kwargs.get("client") is None

        # build_runtime received the CONFIGURED DEPLOY_GRANTS entry — not
        # definition.permissions, the role's full declared set.
        _, kwargs = mock_build_runtime.call_args
        assert tuple(kwargs["granted_permissions"]) == ("read:catalog", "write:orders")


@pytest.mark.asyncio
async def test_lifespan_boot_fails_without_deploy_grants_entry() -> None:
    """spec: 'Boot failure without an explicit grant' (issue #38) — a
    configured runtime with no DEPLOY_GRANTS entry fails boot loudly,
    naming the runtime/role and the env var, instead of defaulting to an
    empty or full-role-set grant."""
    test_settings = _make_settings(deploy_grants={})  # no entry for sales-agent

    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.untrusted_input = False
    fake_definition.execution_limits = None

    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(MagicMock()),
        ),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch("agents_system.harness.loader.resolve", return_value=fake_definition),
        pytest.raises(DefinitionError) as exc_info,
    ):
        app = create_test_app()
        async with lifespan(app):
            pass

    message = str(exc_info.value)
    assert "'sales'" in message
    assert "DEPLOY_GRANTS" in message


@pytest.mark.asyncio
async def test_lifespan_boot_fails_for_untrusted_role_granted_t3_permission() -> None:
    """PR #55 security review (HIGH) — spec.md R4 scenario 'Untrusted role
    cannot be equipped with a T3 grant at deploy time': the REAL
    sales-agent role declares untrusted_input: true. A DEPLOY_GRANTS entry
    granting it a T3-tier permission (exec:command) must fail boot loudly
    (UntrustedInputGrantError propagating out of build_runtime), not equip
    the runtime silently. `harness.loader.resolve` is deliberately left
    unpatched so the REAL resolved untrusted_input applies."""
    test_settings = _make_settings(deploy_grants={"sales": ("exec:command",)})

    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(MagicMock()),
        ),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        pytest.raises(UntrustedInputGrantError),
    ):
        app = create_test_app()
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_lifespan_builds_whatsapp_client() -> None:
    """lifespan builds a WhatsAppClient and stores it on app.state."""
    test_settings = _make_settings(adapter_runtimes=[])
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
    ):
        app = create_test_app()

        async with lifespan(app):
            from agents_system.integration.whatsapp_client import WhatsAppClient

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
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(fake_checkpointer),
        ) as mock_build_checkpointer_cm,
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch("agents_system.harness.loader.resolve", return_value=fake_definition),
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ),
        patch("agents_system.agent.graph.AgentRuntime") as mock_agent_runtime,
    ):
        app = create_test_app()

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
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.main._build_checkpointer_cm"
        ) as mock_build_checkpointer_cm,
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch("agents_system.harness.loader.resolve", return_value=fake_definition),
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ),
        patch("agents_system.agent.graph.AgentRuntime") as mock_agent_runtime,
    ):
        app = create_test_app()

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
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(fake_checkpointer, aexit_calls),
        ),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch("agents_system.harness.loader.resolve", return_value=fake_definition),
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ),
        patch("agents_system.agent.graph.AgentRuntime", return_value=MagicMock()),
        patch(
            "agents_system.integration.whatsapp_client.WhatsAppClient.aclose",
            new=mock_aclose,
        ),
    ):
        app = create_test_app()

        with pytest.raises(RuntimeError, match="dispose boom"):
            async with lifespan(app):
                pass

        mock_aclose.assert_awaited_once()
        assert "checkpointer_exit" in aexit_calls


# ---------------------------------------------------------------------------
# openai-compatible-provider — _build_chat_model dispatch (spec R3, R6, R7)
# ---------------------------------------------------------------------------


def _openai_compatible_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "openai_compatible_base_url": "https://example.test/v1",
        "openai_compatible_model": "test-model",
        "openai_compatible_api_key": "test-key",
    }
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
        "agents_system.main.get_settings", return_value=_openai_compatible_settings()
    ):
        model = _build_chat_model(provider)

    assert type(model).__name__ == expected_class


def test_build_chat_model_wires_openai_compatible_from_settings() -> None:
    with patch(
        "agents_system.main.get_settings", return_value=_openai_compatible_settings()
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
    with (
        patch("agents_system.main.get_settings", return_value=settings),
        pytest.raises(ValueError, match=expected_env_var) as excinfo,
    ):
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
            "agents_system.main.get_settings",
            return_value=_openai_compatible_settings(openai_compatible_api_key=""),
        ),
        patch("agents_system.main.structlog.get_logger", return_value=fake_logger),
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
        "agents_system.main.get_settings", return_value=_openai_compatible_settings()
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
# #169 (ADR-002 E.18) — Ollama model + base URL read from Settings
# ---------------------------------------------------------------------------


def test_build_chat_model_ollama_uses_configured_model_and_base_url() -> None:
    settings = _make_settings(
        ollama_model="qwen3:8b", ollama_base_url="http://localhost:11500"
    )
    with patch("agents_system.main.get_settings", return_value=settings):
        model = _build_chat_model("ollama")

    assert model.model == "qwen3:8b"
    assert model.base_url == "http://localhost:11500"


def test_build_chat_model_ollama_defaults_leave_base_url_unset() -> None:
    """An empty ollama_base_url must be passed as None, not "" -- ChatOllama
    treats an empty string base_url differently from "not configured"
    (it would try to hit http://, not fall back to its own default host).
    """
    with patch("agents_system.main.get_settings", return_value=_make_settings()):
        model = _build_chat_model("ollama")

    assert model.model == "qwen2.5:3b"
    assert model.base_url is None


# ---------------------------------------------------------------------------
# #139 T1 — WhatsApp runtime timeout must leave 60 seconds before the outbox
# lease expires. The constraint is only relevant to the runtime bound to the
# deferred webhook worker; adapter-only runtimes have no webhook lease.
# ---------------------------------------------------------------------------


WHATSAPP_RUNTIME_ID = "sales"


def _runtime_lease_invariant_patches(
    fake_definition: Any,
) -> tuple[Any, Any, tuple[Any, ...]]:
    """Common lifespan patches for runtime/lease invariant tests.

    The worker itself is mocked so accepted startup cases never create a DB or
    Redis-backed background task.
    """
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    mock_worker = MagicMock()
    mock_worker.start = AsyncMock()
    mock_worker.stop = AsyncMock()
    return (
        mock_engine,
        mock_worker,
        (
            patch("agents_system.main.get_engine", return_value=mock_engine),
            patch("agents_system.main.close_redis_pool", new=AsyncMock()),
            patch("agents_system.main._build_chat_model", return_value=MagicMock()),
            patch(
                "agents_system.services.embeddings.get_embedding_provider",
                return_value=MagicMock(),
            ),
            patch("agents_system.harness.loader.resolve", return_value=fake_definition),
            patch(
                "agents_system.harness.factory.build_runtime", return_value=MagicMock()
            ),
            patch("agents_system.agent.graph.AgentRuntime", return_value=MagicMock()),
            patch(
                "agents_system.services.webhook_worker.DeferredWebhookWorker",
                return_value=mock_worker,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("timeout_s", "boots"),
    [(300, True), (539, True), (540, False)],
)
@pytest.mark.asyncio
async def test_lifespan_enforces_whatsapp_runtime_timeout_within_outbox_lease(
    timeout_s: int, boots: bool
) -> None:
    """WhatsApp runtime timeouts must leave the required 60-second headroom."""
    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_runtime_id=WHATSAPP_RUNTIME_ID,
        whatsapp_checkpointer_enabled=False,
    )
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = {"total_execution_timeout_s": timeout_s}

    _, worker, patches = _runtime_lease_invariant_patches(fake_definition)

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        _stack(patches),
    ):
        app = create_test_app()
        if boots:
            async with lifespan(app):
                assert app.state.runtimes
            worker.start.assert_awaited_once()
            worker.stop.assert_awaited_once()
        else:
            with pytest.raises(ValueError) as excinfo:
                async with lifespan(app):
                    pass
            message = str(excinfo.value)
            assert repr(WHATSAPP_RUNTIME_ID) in message
            assert str(timeout_s) in message
            assert "lease" in message
            assert "600" in message


@pytest.mark.asyncio
async def test_lifespan_accepts_default_whatsapp_runtime_limits() -> None:
    """The default effective limit leaves headroom before the 600-second lease."""
    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_runtime_id=WHATSAPP_RUNTIME_ID,
        whatsapp_checkpointer_enabled=False,
    )
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = None

    _, _, patches = _runtime_lease_invariant_patches(fake_definition)

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        _stack(patches),
    ):
        app = create_test_app()
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
    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_runtime_id=WHATSAPP_RUNTIME_ID,
        whatsapp_checkpointer_enabled=False,
    )
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = execution_limits

    _, _, patches = _runtime_lease_invariant_patches(fake_definition)

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        _stack(patches),
    ):
        app = create_test_app()
        async with lifespan(app):
            assert app.state.runtimes


@pytest.mark.asyncio
async def test_lifespan_does_not_apply_webhook_lease_to_adapter_only_runtime() -> None:
    """An adapter-only runtime is not constrained by the webhook worker lease."""
    test_settings = _make_settings(
        adapter_runtimes=[WHATSAPP_RUNTIME_ID],
        whatsapp_runtime_id="",
        whatsapp_checkpointer_enabled=False,
    )
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = {"total_execution_timeout_s": 601}

    _, worker, patches = _runtime_lease_invariant_patches(fake_definition)

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        _stack(patches),
    ):
        app = create_test_app()
        async with lifespan(app):
            assert app.state.runtimes

    worker.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_lifespan_uses_full_lease_duration_when_it_exceeds_one_day() -> None:
    """A long configured lease must not wrap at a day boundary."""
    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_runtime_id=WHATSAPP_RUNTIME_ID,
        whatsapp_checkpointer_enabled=False,
    )
    definition = MagicMock()
    definition.permissions = ("read:catalog",)
    definition.execution_limits = {"total_execution_timeout_s": 601}
    _, _, patches = _runtime_lease_invariant_patches(definition)

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.DEFAULT_LEASE_DURATION", timedelta(days=1)),
        _stack(patches),
    ):
        app = create_test_app()
        async with lifespan(app):
            assert app.state.runtimes


# ---------------------------------------------------------------------------
# D-023 — the BI engine and the read-only guarantee it rests on
#
# The BI block lives inside `if settings.adapter_runtimes:`, so these tests
# supply a runtime and patch the heavy dependencies the surrounding branch
# pulls in. A first draft passed `adapter_runtimes=[]`, never reached the
# block at all, and one assertion passed against unwritten code.
# ---------------------------------------------------------------------------


def _bi_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "adapter_runtimes": ["sales"],
        "whatsapp_checkpointer_enabled": False,
        "bi_database_url": "postgresql+asyncpg://bi_readonly:pw@localhost:5432/acme",
    }
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

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> None:
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
        patch("agents_system.main.get_settings", return_value=settings),
        patch(
            "agents_system.main.get_engine",
            side_effect=lambda url: (
                bi_engine if url == settings.bi_database_url else app_engine
            ),
        ),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch("agents_system.harness.loader.resolve", return_value=fake_definition),
        patch("agents_system.harness.factory.build_runtime", return_value=MagicMock()),
        patch("agents_system.agent.graph.AgentRuntime", return_value=MagicMock()),
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
            app = create_test_app(registry_factory=fake_build_registry)
            async with lifespan(app):
                pass
        # NOT captured.get(): a builder that was never called would return
        # None, which is indistinguishable from the fail-closed result the
        # "role can write" test asserts. Missing means the patch target is
        # wrong, and that must be a failure, not a pass.
        assert "bi_engine" in captured, "registry_factory was never called"
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

    with patches[0], patches[1] as mock_get, _stack(patches[2:]):
        app = create_test_app()
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


# ---------------------------------------------------------------------------
# Composition: the app factory takes its wiring from the caller
# ---------------------------------------------------------------------------


async def test_create_app_boots_with_a_caller_supplied_registry() -> None:
    """A consumer supplies its own registry factory and never edits the library.

    This is the seam the client repository takes over: a consumer passes its
    own wiring through `create_app(...)`, and nothing about that call is
    privileged. The factory here registers a tool that exists in no
    deployment in this repository, and the lifespan calls it with the settings,
    embedder and BI engine it resolved.
    """
    from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec
    from agents_system.main import create_app

    calls: list[tuple[Any, Any]] = []

    def consumer_registry(
        settings: Any, embedder: Any = None, bi_engine: Any = None
    ) -> ToolRegistry:
        calls.append((embedder, bi_engine))
        registry = ToolRegistry()
        registry.register(
            ToolSpec(
                name="consumer_owned_tool",
                required_permissions=(),
                connector=lambda inputs: {"ok": True},
                tier=Tier.T0,
            )
        )
        return registry

    application = create_app(registry_factory=consumer_registry, title="Consumer App")

    assert application.title == "Consumer App"
    assert application.state.registry_factory is consumer_registry
    # Absent by default: the platform owns no identity schema.
    assert application.state.participant_directory is None
    assert application.state.conversation_recorder is None

    # The factory is the one the lifespan would call, and it builds a registry
    # holding only what the consumer registered.
    registry = application.state.registry_factory(object())
    assert registry.names() == ("consumer_owned_tool",)
    assert len(calls) == 1


def test_app_version_matches_package_version() -> None:
    import agents_system

    app = create_test_app()

    assert app.version == agents_system.__version__


def test_create_app_requires_a_registry_factory() -> None:
    """No default registry: an application must say what it boots with.

    A default would be the platform silently choosing one deployment's
    connectors, which is exactly the coupling this seam removes.
    """
    from agents_system.main import create_app

    with pytest.raises(TypeError):
        create_app()  # type: ignore[call-arg]


async def test_create_app_stores_the_ports_it_is_given() -> None:
    """Supplied ports must actually reach `app.state`, not just default to None.

    Found by adversarial review: the test above pinned only the DEFAULTS, so
    `create_app` could drop both arguments on the floor and all 647 tests
    stayed green. These two objects decide whether the inbound route runs a
    turn at all -- silently discarding them is the failure this asserts
    against.
    """
    from agents_system.harness.registry import ToolRegistry
    from agents_system.main import create_app

    directory = object()
    recorder = object()

    application = create_app(
        registry_factory=lambda *a, **k: ToolRegistry(),
        participant_directory=directory,  # type: ignore[arg-type]
        conversation_recorder=recorder,  # type: ignore[arg-type]
    )

    assert application.state.participant_directory is directory
    assert application.state.conversation_recorder is recorder


def test_create_app_accepts_and_stores_roots_on_app_state() -> None:
    """`create_app` exposes an explicit `roots: RootConfig | None = None` and stores it on app.state."""
    from agents_system.harness.loader import RootConfig
    from agents_system.harness.registry import ToolRegistry
    from agents_system.main import create_app

    custom_roots = RootConfig()
    app = create_app(
        registry_factory=lambda *a, **k: ToolRegistry(),
        roots=custom_roots,
    )
    assert app.state.roots is custom_roots

    app_default = create_app(
        registry_factory=lambda *a, **k: ToolRegistry(),
    )
    assert app_default.state.roots is None


async def test_the_lifespan_calls_the_caller_supplied_registry_factory() -> None:
    """The factory has to be the one the lifespan actually invokes.

    Previously only covered incidentally, and that coverage was anchored to
    `agents_system.connectors.rag_connector` -- the module #70 deletes next. This
    drives the real lifespan and asserts the caller's factory was called with
    the settings, embedder and BI engine it resolved.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from agents_system.harness.registry import ToolRegistry
    from agents_system.main import create_app, lifespan

    calls: list[tuple[Any, ...]] = []

    def consumer_registry(
        settings: Any, embedder: Any = None, bi_engine: Any = None
    ) -> ToolRegistry:
        calls.append((settings, embedder, bi_engine))
        return ToolRegistry()

    application = create_app(registry_factory=consumer_registry)

    # The runtime cache is gated on `adapter_runtimes` being non-empty, so it
    # has to be set for the lifespan to reach any factory at all. That gate is
    # itself questionable -- it makes an OpenAI-adapter setting decide whether
    # the WhatsApp channel has runtimes -- but it is not this test's subject.
    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        agent_registrations={"sales": "sales-agent"},
        adapter_runtimes=["sales"],
    )

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=MagicMock()),
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        # Without this the lifespan constructs a real LocalBGEEmbeddingProvider,
        # whose __init__ downloads 4.3 GB from HuggingFace — making this the
        # only test in the default suite that needs live network. pyproject
        # defines an `integration` marker for exactly that and deselects it by
        # default; a unit test must not quietly opt back in.
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        try:
            async with lifespan(application):
                pass
        except Exception as exc:
            # Narrow enough to stay useful: the lifespan builds far more than
            # the registry, and a later failure does not un-call the factory —
            # but a swallowed cause that leaves `calls` empty would otherwise
            # be reported as "the seam is broken", pointing a reader at
            # main.py when the real cause was upstream.
            lifespan_error: Exception | None = exc
        else:
            lifespan_error = None

    assert calls, "the lifespan never called the caller-supplied factory" + (
        f" (it died first: {lifespan_error!r})" if lifespan_error else ""
    )
    # The docstring claims the factory receives what the lifespan resolved,
    # so assert it rather than only that the list is non-empty.
    settings_seen, _embedder_seen, _bi_seen = calls[0]
    assert settings_seen is test_settings


# ---------------------------------------------------------------------------
# The runtime cache serves every channel, not just the OpenAI adapter
# ---------------------------------------------------------------------------


async def test_whatsapp_runtime_is_built_even_with_no_adapter_runtimes() -> None:
    """An adapter setting must not decide whether WhatsApp has a runtime.

    The cache used to be gated on `if settings.adapter_runtimes:` alone. Once
    that default became empty, `app.state.runtimes` stayed `{}`, the inbound
    route found nothing for `whatsapp_runtime_id`, and every message got a
    200 with no turn — visible only as a `webhook.runtime_unresolved`
    warning. Two unrelated features shared one switch.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from agents_system.harness.registry import ToolRegistry
    from agents_system.main import create_app, lifespan

    built: list[tuple[Any, Any]] = []

    def _awaitable_engine() -> MagicMock:
        engine = MagicMock()
        engine.dispose = AsyncMock()
        return engine

    def spy_build_runtime(*args: Any, **kwargs: Any) -> Any:
        role = kwargs.get("role_type") or (args[0] if args else None)
        built.append((role, kwargs.get("client")))
        raise RuntimeError("stop here — the call itself is what is asserted")

    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        agent_registrations={"wa-sales": "sales-agent"},
        adapter_runtimes=[],  # nothing published on /v1 ...
        whatsapp_runtime_id="wa-sales",  # ... but WhatsApp needs one
        deploy_grants={"wa-sales": ("read:catalog",)},
        whatsapp_checkpointer_enabled=False,
        embedding_provider="openai",
        openai_api_key="test-key",
    )
    application = create_app(registry_factory=lambda *a, **k: ToolRegistry())

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=_awaitable_engine()),
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agents_system.harness.factory.build_runtime", side_effect=spy_build_runtime
        ),
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        try:
            async with lifespan(application):
                pass
        except RuntimeError as exc:
            assert "stop here" in str(exc), exc

    assert built == [("sales-agent", None)], (
        "the WhatsApp runtime was not built; adapter_runtimes gated it"
    )


async def test_lifespan_passes_explicit_roots_to_build_runtime_too() -> None:
    """Both call sites in the loop must use the caller-supplied explicit root.

    The caller supplies a valid fixture RootConfig to create_app.
    lifespan passes the exact caller-supplied RootConfig object to BOTH
    `resolve` and `build_runtime` for the client override runtime.
    """
    import pathlib as _pathlib
    from unittest.mock import AsyncMock, MagicMock, patch

    from agents_system.harness.loader import RootConfig
    from agents_system.harness.registry import ToolRegistry
    from agents_system.main import create_app, lifespan

    resolve_roots: list[Any] = []
    build_runtime_roots: list[Any] = []

    def _awaitable_engine() -> MagicMock:
        engine = MagicMock()
        engine.dispose = AsyncMock()
        return engine

    def spy_resolve(*args: Any, **kwargs: Any) -> Any:
        resolve_roots.append(kwargs.get("roots"))
        fake_def = MagicMock()
        fake_def.permissions = ("read:catalog",)
        fake_def.execution_limits = None
        return fake_def

    def spy_build_runtime(*args: Any, **kwargs: Any) -> Any:
        build_runtime_roots.append(kwargs.get("roots"))
        raise RuntimeError("stop here — the call itself is what is asserted")

    fixture_deployments = (
        _pathlib.Path(__file__).resolve().parent
        / "fixtures"
        / "agents"
        / "overrides"
        / "deployments"
    )
    fixture_roots = RootConfig(deployments_root=fixture_deployments)

    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        agent_registrations={"acme-sales": "sales-agent@client-a"},
        adapter_runtimes=["acme-sales"],
        deploy_grants={"acme-sales": ("read:catalog",)},
        whatsapp_checkpointer_enabled=False,
        embedding_provider="openai",
        openai_api_key="test-key",
    )
    application = create_app(
        registry_factory=lambda *a, **k: ToolRegistry(),
        roots=fixture_roots,
    )

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=_awaitable_engine()),
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch("agents_system.harness.loader.resolve", side_effect=spy_resolve),
        patch(
            "agents_system.harness.factory.build_runtime", side_effect=spy_build_runtime
        ),
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        try:
            async with lifespan(application):
                pass
        except RuntimeError as exc:
            assert "stop here" in str(exc), exc

    assert resolve_roots, "resolve was never reached"
    assert resolve_roots[0] is fixture_roots, (
        f"resolve got {resolve_roots[0]!r}, expected {fixture_roots!r}"
    )
    assert build_runtime_roots, "build_runtime was never reached"
    assert build_runtime_roots[0] is fixture_roots, (
        f"build_runtime got {build_runtime_roots[0]!r}, expected {fixture_roots!r}"
    )
    assert build_runtime_roots[0].deployments_root == fixture_deployments
    assert fixture_deployments.is_dir(), "the fixture path must actually exist"


async def test_client_runtime_without_explicit_roots_raises_definition_error() -> None:
    """A "{role}@{client}" registration with no explicit roots raises
    DefinitionError.

    agents_system does not derive a default deployments root; a consumer must pass
    RootConfig explicitly to create_app.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from agents_system.harness.loader import DefinitionError
    from agents_system.harness.registry import ToolRegistry
    from agents_system.main import create_app, lifespan

    def _awaitable_engine() -> MagicMock:
        engine = MagicMock()
        engine.dispose = AsyncMock()
        return engine

    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        agent_registrations={"acme-sales": "sales-agent@client-a"},
        adapter_runtimes=["acme-sales"],
        whatsapp_checkpointer_enabled=False,
        embedding_provider="openai",
        openai_api_key="test-key",
    )
    application = create_app(registry_factory=lambda *a, **k: ToolRegistry())

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=_awaitable_engine()),
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        with pytest.raises(DefinitionError) as exc_info:
            async with lifespan(application):
                pass

        assert "'acme-sales'" in str(exc_info.value)
        assert "'client-a'" in str(exc_info.value)


async def test_client_whatsapp_runtime_without_explicit_roots_raises_definition_error() -> (
    None
):
    """A client override in whatsapp_runtime_id with no roots also raises DefinitionError."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from agents_system.harness.loader import DefinitionError
    from agents_system.harness.registry import ToolRegistry
    from agents_system.main import create_app, lifespan

    def _awaitable_engine() -> MagicMock:
        engine = MagicMock()
        engine.dispose = AsyncMock()
        return engine

    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        agent_registrations={"wa-sales": "sales-agent@client-a"},
        adapter_runtimes=[],
        whatsapp_runtime_id="wa-sales",
        whatsapp_checkpointer_enabled=False,
        embedding_provider="openai",
        openai_api_key="test-key",
    )
    application = create_app(registry_factory=lambda *a, **k: ToolRegistry())

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=_awaitable_engine()),
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        with pytest.raises(DefinitionError) as exc_info:
            async with lifespan(application):
                pass

        assert "'wa-sales'" in str(exc_info.value)
        assert "'client-a'" in str(exc_info.value)


async def test_generic_runtime_boots_without_explicit_roots() -> None:
    """A registration with no client ("{role}") needs no explicit roots."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from agents_system.harness.registry import ToolRegistry
    from agents_system.main import create_app, lifespan

    def _awaitable_engine() -> MagicMock:
        engine = MagicMock()
        engine.dispose = AsyncMock()
        return engine

    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        agent_registrations={"sales": "sales-agent"},
        adapter_runtimes=["sales"],
        deploy_grants={"sales": ("read:catalog",)},
        whatsapp_checkpointer_enabled=False,
        embedding_provider="openai",
        openai_api_key="test-key",
    )
    application = create_app(registry_factory=lambda *a, **k: ToolRegistry())

    fake_equipped = MagicMock()
    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=_awaitable_engine()),
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ),
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        async with lifespan(application):
            assert "sales" in application.state.runtimes


# ---------------------------------------------------------------------------
# ADR-002 C.13 — channel-to-role untrusted_input boot check
#
# `resolve()` is left unpatched in these three tests on purpose: the whole
# point is that the check reads the REAL resolved untrusted_input of a real
# platform role (`operator-agent` = False, `sales-agent` = True — see
# tests/test_untrusted_input_invariant.py's _EXPECTED_UNTRUSTED_INPUT table),
# not a mock that could drift from what `resolve()` actually returns.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_app_refuses_to_boot_when_whatsapp_role_is_not_untrusted_input() -> (
    None
):
    """A WhatsApp-bound role that resolves untrusted_input=False must refuse
    to boot, naming the role and the channel, before build_runtime runs and
    before app.state.runtimes is ever populated (boot failure, not a runtime
    surprise)."""
    from agents_system.harness.loader import DefinitionError

    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_runtime_id="operator",
        whatsapp_checkpointer_enabled=False,
    )
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    build_runtime_calls: list[Any] = []

    def spy_build_runtime(*args: Any, **kwargs: Any) -> Any:
        build_runtime_calls.append(kwargs)
        raise AssertionError(
            "build_runtime must not be reached — the boot check runs first"
        )

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agents_system.harness.factory.build_runtime", side_effect=spy_build_runtime
        ),
    ):
        app = create_test_app()

        with pytest.raises(DefinitionError, match="operator-agent") as exc_info:
            async with lifespan(app):
                pass

        assert "whatsapp" in str(exc_info.value).lower()
        assert not build_runtime_calls, (
            "build_runtime ran before the untrusted_input boot check"
        )
        assert not hasattr(app.state, "runtimes"), (
            "app.state.runtimes was populated before the refusal — the "
            "check must run before the cache is ever assigned"
        )


@pytest.mark.asyncio
async def test_create_app_boots_when_whatsapp_role_is_untrusted_input_true() -> None:
    """The counterpart of the refusal test above: sales-agent resolves
    untrusted_input=True, so binding it to WhatsApp boots normally."""
    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_runtime_id="sales",
        whatsapp_checkpointer_enabled=False,
    )
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    fake_equipped = MagicMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ),
        patch("agents_system.agent.graph.AgentRuntime", return_value=MagicMock()),
    ):
        app = create_test_app()

        async with lifespan(app):
            assert "sales" in app.state.runtimes


@pytest.mark.asyncio
async def test_boot_check_does_not_apply_to_adapter_only_runtimes() -> None:
    """Regression — the OpenAI adapter is an accepted risk (ADR-002 C.13),
    not enforced by this check: an untrusted_input=False role published only
    through adapter_runtimes (no whatsapp_runtime_id) still boots."""
    test_settings = _make_settings(
        adapter_runtimes=["operator"],
        whatsapp_runtime_id="",
        whatsapp_checkpointer_enabled=False,
    )
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    fake_equipped = MagicMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ),
        patch("agents_system.agent.graph.AgentRuntime", return_value=MagicMock()),
    ):
        app = create_test_app()

        async with lifespan(app):
            assert "operator" in app.state.runtimes


# ---------------------------------------------------------------------------
# W2b2 — the deferred webhook worker's lifespan wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_the_webhook_worker_around_dependencies() -> (
    None
):
    """The worker is constructed only once engine/whatsapp_client/runtimes
    already exist on app.state, is started before ``yield``, and is stopped
    before the engine is disposed and the WhatsApp client is closed --
    AsyncExitStack's LIFO teardown runs the worker's own stop() first among
    the callbacks pushed so far, ahead of the dependencies it used."""
    test_settings = _make_settings(
        whatsapp_runtime_id="sales",
        whatsapp_checkpointer_enabled=False,
    )
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = None
    fake_equipped = MagicMock()

    events: list[str] = []

    mock_engine = MagicMock()

    async def dispose() -> None:
        events.append("engine.dispose")

    mock_engine.dispose = dispose

    async def aclose(self: object) -> None:
        events.append("whatsapp.aclose")

    fake_worker = MagicMock()

    async def worker_start() -> None:
        events.append("worker.start")

    async def worker_stop() -> None:
        events.append("worker.stop")

    fake_worker.start = AsyncMock(side_effect=worker_start)
    fake_worker.stop = AsyncMock(side_effect=worker_stop)

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch("agents_system.harness.loader.resolve", return_value=fake_definition),
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ),
        patch("agents_system.agent.graph.AgentRuntime", return_value=MagicMock()),
        patch(
            "agents_system.integration.whatsapp_client.WhatsAppClient.aclose",
            new=aclose,
        ),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker",
            return_value=fake_worker,
        ) as mock_worker_cls,
    ):
        app = create_test_app()

        async with lifespan(app):
            assert app.state.runtimes
            # Constructed with the dependencies it needs already in place.
            _, kwargs = mock_worker_cls.call_args
            assert kwargs["runtime"] is app.state.runtimes["sales"]
            assert kwargs["whatsapp_client"] is app.state.whatsapp_client
            assert kwargs["directory"] is app.state.participant_directory
            assert kwargs["recorder"] is app.state.conversation_recorder
            # #46 — the SAME limiter instance the lifespan exposes on
            # app.state (and that the OpenAI adapter reads at request time)
            # is what bounds this worker's concurrent processing too.
            assert kwargs["admission_limiter"] is app.state.turn_admission_limiter
            assert (
                app.state.turn_admission_limiter.max_concurrent_turns
                == test_settings.max_concurrent_turns
            )

            fake_worker.start.assert_awaited_once()
            assert app.state.webhook_worker is fake_worker
            assert "worker.stop" not in events

        assert events == [
            "worker.start",
            "worker.stop",
            "whatsapp.aclose",
            "engine.dispose",
        ]


@pytest.mark.asyncio
async def test_lifespan_skips_the_webhook_worker_when_no_runtime_is_resolved() -> None:
    """Unset whatsapp_runtime_id, with no WhatsApp credentials configured
    either (the platform default) must not start a worker with no runtime
    to hand it -- durable inbound work then simply waits unprocessed,
    matching the webhook route's own no-runtime handling. #141: this stays a
    warning, not a boot failure, because WHATSAPP_TOKEN/
    WHATSAPP_PHONE_NUMBER_ID are also unset -- there is nothing configured
    to receive-and-reply through in the first place."""
    test_settings = _make_settings(adapter_runtimes=[])
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker"
        ) as mock_worker_cls,
    ):
        app = create_test_app()

        async with lifespan(app):
            assert app.state.webhook_worker is None
            # #46 — the admission limiter is not the webhook worker's: it
            # must still exist so POST /v1/chat/completions stays bounded
            # even when no WhatsApp runtime is configured at all.
            assert app.state.turn_admission_limiter is not None

        mock_worker_cls.assert_not_called()


# ---------------------------------------------------------------------------
# #141 — boot fails closed when WhatsApp credentials are configured but
# whatsapp_runtime_id resolves to no runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lifespan_fails_closed_when_runtime_id_is_empty_with_credentials() -> (
    None
):
    """WHATSAPP_TOKEN + WHATSAPP_PHONE_NUMBER_ID configured, but
    whatsapp_runtime_id left unset -- boot must refuse rather than accept
    signed inbound messages nothing will ever process."""
    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_token="test-token",
        whatsapp_phone_number_id="1234567890",
        whatsapp_runtime_id="",
    )
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker"
        ) as mock_worker_cls,
    ):
        app = create_test_app()

        with pytest.raises(DefinitionError, match="whatsapp_runtime_id"):
            async with lifespan(app):
                pass

        mock_worker_cls.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_fails_closed_when_runtime_id_is_unregistered_with_credentials() -> (
    None
):
    """A whatsapp_runtime_id that AGENT_REGISTRATIONS does not register is
    the other #141 review-comment case: it resolves to no runtime just as
    surely as an unset one, and must fail boot too, naming the id (ADR-004
    PR4b: there is no id format left to be malformed -- only unregistered)."""
    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_token="test-token",
        whatsapp_phone_number_id="1234567890",
        whatsapp_runtime_id="not-registered",
        whatsapp_checkpointer_enabled=False,
    )
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker"
        ) as mock_worker_cls,
    ):
        app = create_test_app()

        with pytest.raises(
            DefinitionError,
            match="WHATSAPP_RUNTIME_ID names runtime id.*'not-registered'",
        ):
            async with lifespan(app):
                pass

        mock_worker_cls.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_boots_with_only_one_whatsapp_credential_set() -> None:
    """The #141 boot check is scoped to BOTH WHATSAPP_TOKEN and
    WHATSAPP_PHONE_NUMBER_ID being set -- a deployment with only one (e.g.
    mid-migration, or genuinely partially configured) is not yet a complete
    'ready to receive and reply' WhatsApp setup, so it keeps the pre-#141
    warn-and-boot behaviour rather than a hard failure."""
    test_settings = _make_settings(
        adapter_runtimes=[],
        whatsapp_token="test-token",
        whatsapp_phone_number_id="",
        whatsapp_runtime_id="",
    )
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker"
        ) as mock_worker_cls,
    ):
        app = create_test_app()

        async with lifespan(app):
            assert app.state.webhook_worker is None

        mock_worker_cls.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_boots_normally_with_credentials_and_valid_runtime_id() -> None:
    """Regression guard: WhatsApp credentials configured AND a well-formed,
    resolvable whatsapp_runtime_id must still boot and start the worker --
    #141's check must not fire on the healthy path."""
    test_settings = _make_settings(
        whatsapp_token="test-token",
        whatsapp_phone_number_id="1234567890",
        whatsapp_runtime_id="sales",
        whatsapp_checkpointer_enabled=False,
    )
    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog",)
    fake_definition.execution_limits = None
    fake_equipped = MagicMock()
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agents_system.main.get_settings", return_value=test_settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agents_system.harness.factory.build_runtime", return_value=fake_equipped
        ),
        patch("agents_system.agent.graph.AgentRuntime", return_value=MagicMock()),
    ):
        app = create_test_app()

        async with lifespan(app):
            assert app.state.webhook_worker is not None


# ---------------------------------------------------------------------------
# ADR-004 PR4a-ii — `lifespan()` serves `create_app(agents=, grants=, clients=)`
#
# Every test in this section has "registration" in its name, so
# `pytest tests/test_main.py -k registration` runs exactly this slice.
# `resolve`/`build_runtime` are REAL unless a test says otherwise; only
# `AgentRuntime` is replaced, by a stub that keeps its kwargs, so a test can
# read back the `EquippedRuntime` the lifespan actually built.
# ---------------------------------------------------------------------------


_FIXTURE_DEPLOYMENTS = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures"
    / "agents"
    / "overrides"
    / "deployments"
)
_SALES_GRANT = ("read:catalog", "write:orders")


def _registration_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "allow_insecure": True,
        "database_url": "postgresql+asyncpg://localhost:5432/agentsys_test",
        "redis_url": "redis://localhost:6379/0",
        "adapter_runtimes": [],
        "whatsapp_checkpointer_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


_REGISTRATION_ENV_VARS = (
    "AGENT_REGISTRATIONS",
    "ADAPTER_RUNTIMES",
    "WHATSAPP_RUNTIME_ID",
    "DEPLOY_GRANTS",
)


def _env_settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Settings read from the process environment, the way an operator
    configures the Settings-driven boot: only the variables in `env` are
    set, every other registration variable is cleared."""
    for name in _REGISTRATION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
        whatsapp_checkpointer_enabled=False,
    )


def _registration_patches(settings: Settings) -> tuple[Any, ...]:
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    return (
        patch("agents_system.main.get_settings", return_value=settings),
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        # The stub keeps what it was given: runtimes[id]["runtime"] is the
        # EquippedRuntime build_runtime returned for that id.
        patch("agents_system.agent.graph.AgentRuntime", side_effect=lambda **kw: kw),
    )


async def _boot(app: Any, settings: Settings, *extra: Any) -> None:
    with _stack(_registration_patches(settings)), _stack(extra):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_registration_of_a_predefined_role_matches_the_env_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec: 'A predefined role is registered under a deployer-chosen id' --
    `agents={"acme-sales": "sales-agent"}` + `clients={"acme-sales":
    "client-a"}` builds the same runtime, field for field, that the
    Settings-driven boot builds from `AGENT_REGISTRATIONS='{"acme-sales":
    "sales-agent@client-a"}'` for the same role, client and grant: both paths
    run one loop (ADR-004 PR4b-T2)."""
    roots = RootConfig(deployments_root=_FIXTURE_DEPLOYMENTS)

    from_env = create_test_app(roots=roots)
    await _boot(
        from_env,
        _env_settings(
            monkeypatch,
            AGENT_REGISTRATIONS='{"acme-sales": "sales-agent@client-a"}',
            ADAPTER_RUNTIMES='["acme-sales"]',
            DEPLOY_GRANTS='{"acme-sales": ["read:catalog", "write:orders"]}',
        ),
    )
    registered = create_test_app(
        roots=roots,
        agents={"acme-sales": "sales-agent"},
        grants={"acme-sales": list(_SALES_GRANT)},
        clients={"acme-sales": "client-a"},
    )
    await _boot(registered, _registration_settings(adapter_runtimes=["acme-sales"]))

    assert set(from_env.state.runtimes) == {"acme-sales"}
    assert set(registered.state.runtimes) == {"acme-sales"}
    old = from_env.state.runtimes["acme-sales"]["runtime"]
    new = registered.state.runtimes["acme-sales"]["runtime"]
    assert new.definition == old.definition
    assert new.definition.deployment == "client-a"
    assert new.system_prompt == old.system_prompt
    assert [t.name for t in new.tools] == [t.name for t in old.tools]
    assert new.denied_tools == old.denied_tools
    assert new.skills == old.skills
    assert new.deploy_grant_ceiling == old.deploy_grant_ceiling
    assert registered.state.adapter_model_ids == frozenset({"acme-sales"})
    assert from_env.state.adapter_model_ids == frozenset({"acme-sales"})


@pytest.mark.parametrize("bad_id", ["", "bad id", "-leading-hyphen"])
@pytest.mark.asyncio
async def test_registration_rejects_an_invalid_id_before_any_runtime_is_built(
    bad_id: str,
) -> None:
    """spec: 'An empty-string id is rejected' -- raised before build_runtime
    runs, naming the offending id."""
    app = create_test_app(
        agents={"acme-sales": "sales-agent", bad_id: "sales-agent"},
        grants={"acme-sales": _SALES_GRANT, bad_id: _SALES_GRANT},
    )
    build_runtime = MagicMock(side_effect=AssertionError("must not be reached"))

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(
            app,
            _registration_settings(),
            patch("agents_system.harness.factory.build_runtime", build_runtime),
        )

    assert repr(bad_id) in str(exc_info.value)
    build_runtime.assert_not_called()
    assert not hasattr(app.state, "runtimes")


@pytest.mark.parametrize(
    ("runtime_id", "role"),
    [("acme-support-v2", "sales-agent"), ("acme__sales-agent", "operator-agent")],
)
@pytest.mark.asyncio
async def test_registration_accepts_an_arbitrary_id_without_parsing_it(
    runtime_id: str, role: str
) -> None:
    """spec: 'An arbitrary id string with no embedded convention is accepted'
    -- the id is the cache key as given. Even a legacy-shaped id is never
    split: `acme__sales-agent` here serves operator-agent, with no client."""
    app = create_test_app(
        agents={runtime_id: role},
        grants={runtime_id: ("read:catalog",)},
    )

    await _boot(app, _registration_settings())

    assert set(app.state.runtimes) == {runtime_id}
    definition = app.state.runtimes[runtime_id]["runtime"].definition
    assert definition.role_name == role
    assert definition.deployment is None


def _write_agent_folder(
    base: pathlib.Path, name: str, *, untrusted_input: bool = True
) -> pathlib.Path:
    """A minimal importer-agent folder: the three-file contract, no `extends:`."""
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n# Role: {name}\n\nProse body.\n',
        encoding="utf-8",
    )
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\ntools: [catalog_search]\nskills: []\n'
        "context: {}\npermissions:\n  - read:catalog\n---\n\nManifest body.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nautonomy: supervised\n'
        f"untrusted_input: {str(untrusted_input).lower()}\n"
        "execution_limits: null\n---\n\nPolicy body.\n",
        encoding="utf-8",
    )
    return folder


@pytest.mark.asyncio
async def test_registration_serves_a_custom_folder_agent_on_both_channels(
    tmp_path: pathlib.Path,
) -> None:
    """spec: 'An importer-defined custom agent is registered and served' --
    an `Agent.from_folder(...)` registration is built, bound to WhatsApp and
    published on /v1/models through the same code paths a predefined role
    uses."""
    import httpx

    from agents_system.agent.spec import Agent

    triage = Agent.from_folder(_write_agent_folder(tmp_path, "triage-bot"))
    app = create_test_app(
        agents={"triage-bot": triage}, grants={"triage-bot": ["read:catalog"]}
    )
    settings = _registration_settings(
        adapter_runtimes=["triage-bot"], whatsapp_runtime_id="triage-bot"
    )
    worker = MagicMock()
    worker.start = AsyncMock()
    worker.stop = AsyncMock()

    with (
        _stack(_registration_patches(settings)),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker",
            return_value=worker,
        ) as worker_cls,
        patch(
            "agents_system.integration.openai_adapter.get_settings",
            return_value=settings,
        ),
        # The request below is audited; keep that off the MagicMock engine.
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        async with lifespan(app):
            equipped = app.state.runtimes["triage-bot"]["runtime"]
            assert equipped.definition.role_name == "triage-bot"
            assert [t.name for t in equipped.tools] == ["catalog_search"]
            _, kwargs = worker_cls.call_args
            assert kwargs["runtime"] is app.state.runtimes["triage-bot"]

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get("/v1/models")

    assert response.status_code == 200
    assert [m["id"] for m in response.json()["data"]] == ["triage-bot"]


@pytest.mark.asyncio
async def test_registration_rejects_clients_for_an_agent_valued_entry() -> None:
    """design.md D5: `clients` is only meaningful for a `str` (predefined
    role) entry. Pairing it with an `Agent` fails boot rather than being
    silently ignored."""
    from agents_system.agent.spec import Agent

    app = create_test_app(
        agents={"triage-bot": Agent(name="triage-bot")},
        grants={"triage-bot": ["read:session"]},
        clients={"triage-bot": "client-a"},
    )

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(app, _registration_settings())

    message = str(exc_info.value)
    assert "triage-bot" in message
    assert "clients" in message
    assert not hasattr(app.state, "runtimes")


@pytest.mark.parametrize(
    ("agents", "clients"),
    [
        # A typo'd key would silently drop a subtractive deployment override.
        ({"acme-sales": "sales-agent"}, {"acme-sale": "client-a"}),
        # `clients` has nothing to apply to without `agents`.
        (None, {"acme-sales": "client-a"}),
    ],
)
@pytest.mark.asyncio
async def test_registration_rejects_clients_for_an_unregistered_id(
    agents: dict[str, str] | None, clients: dict[str, str]
) -> None:
    app = create_test_app(
        agents=agents,
        grants={"acme-sales": _SALES_GRANT},
        clients=clients,
        roots=RootConfig(deployments_root=_FIXTURE_DEPLOYMENTS),
    )

    with pytest.raises(DefinitionError, match="clients"):
        await _boot(app, _registration_settings())

    assert not hasattr(app.state, "runtimes")


@pytest.mark.parametrize(
    "bad_client",
    [
        # `os.environ.get("ACME_CLIENT")` with the variable unset: the role
        # would be served generic, without its subtractive override.
        None,
        42,
        "",
        "../client-a",
    ],
)
@pytest.mark.asyncio
async def test_registration_rejects_a_clients_value_that_is_not_a_client_name(
    bad_client: object,
) -> None:
    """A `clients` value that is not a client-name str fails boot naming the
    id, before any runtime is built, and without echoing the value."""
    app = create_test_app(
        agents={"acme-sales": "sales-agent"},
        grants={"acme-sales": _SALES_GRANT},
        clients={"acme-sales": bad_client},  # type: ignore[dict-item]
        roots=RootConfig(deployments_root=_FIXTURE_DEPLOYMENTS),
    )
    build_runtime = MagicMock(side_effect=AssertionError("must not be reached"))

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(
            app,
            _registration_settings(adapter_runtimes=["acme-sales"]),
            patch("agents_system.harness.factory.build_runtime", build_runtime),
        )

    message = str(exc_info.value)
    assert "clients['acme-sales']" in message
    if isinstance(bad_client, str) and bad_client:
        assert bad_client not in message
    build_runtime.assert_not_called()
    assert not hasattr(app.state, "runtimes")


@pytest.mark.asyncio
async def test_registration_one_unresolvable_entry_blocks_the_whole_boot() -> None:
    """spec: 'One bad registration entry blocks the whole boot' -- the two
    valid entries are not served while the broken one is dropped."""
    from agents_system.agent.spec import Agent

    app = create_test_app(
        agents={
            "acme-sales": "sales-agent",
            "broken-bot": Agent(name="broken-bot", extends="nowhere/custom-agent"),
            "acme-ops": "operator-agent",
        },
        grants={
            "acme-sales": _SALES_GRANT,
            "broken-bot": ["read:session"],
            "acme-ops": ["read:session"],
        },
    )

    with pytest.raises(DefinitionError, match="nowhere/custom-agent"):
        await _boot(app, _registration_settings())

    assert not hasattr(app.state, "runtimes")
    assert not hasattr(app.state, "adapter_model_ids")


def _mock_worker() -> MagicMock:
    worker = MagicMock()
    worker.start = AsyncMock()
    worker.stop = AsyncMock()
    return worker


@pytest.mark.asyncio
async def test_registration_binds_whatsapp_and_publishes_only_adapter_ids() -> None:
    """spec: 'A registered id is correctly bound to WhatsApp', 'An
    adapter-named id appears in /v1/models' and 'A WhatsApp-only runtime does
    not leak into /v1/models' -- the cache holds both runtimes, /v1 lists only
    the id ADAPTER_RUNTIMES names, never every key of `agents`."""
    import httpx

    app = create_test_app(
        agents={"acme-sales": "sales-agent", "support-bot": "sales-agent"},
        grants={"acme-sales": _SALES_GRANT, "support-bot": _SALES_GRANT},
    )
    settings = _registration_settings(
        adapter_runtimes=["acme-sales"], whatsapp_runtime_id="support-bot"
    )

    with (
        _stack(_registration_patches(settings)),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker",
            return_value=_mock_worker(),
        ) as worker_cls,
        patch(
            "agents_system.integration.openai_adapter.get_settings",
            return_value=settings,
        ),
        patch("agents_system.audit.sink.AuditSink") as sink_cls,
    ):
        sink_cls.return_value.start = AsyncMock()
        sink_cls.return_value.stop = AsyncMock()
        async with lifespan(app):
            assert set(app.state.runtimes) == {"acme-sales", "support-bot"}
            assert app.state.adapter_model_ids == frozenset({"acme-sales"})
            _, kwargs = worker_cls.call_args
            assert kwargs["runtime"] is app.state.runtimes["support-bot"]

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get("/v1/models")

    assert [m["id"] for m in response.json()["data"]] == ["acme-sales"]


@pytest.mark.parametrize(
    ("channel", "overrides"),
    [
        ("WHATSAPP_RUNTIME_ID", {"whatsapp_runtime_id": "ghost-bot"}),
        ("ADAPTER_RUNTIMES", {"adapter_runtimes": ["acme-sales", "ghost-bot"]}),
    ],
)
@pytest.mark.asyncio
async def test_registration_fails_boot_on_an_unmatched_channel_id(
    channel: str, overrides: dict[str, object]
) -> None:
    """spec: 'An unmatched WhatsApp runtime id fails boot' -- naming the id.
    An ADAPTER_RUNTIMES id no registration serves fails the same way,
    rather than /v1/models silently listing less than the operator named."""
    app = create_test_app(
        agents={"acme-sales": "sales-agent"}, grants={"acme-sales": _SALES_GRANT}
    )
    build_runtime = MagicMock(side_effect=AssertionError("must not be reached"))

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(
            app,
            _registration_settings(**overrides),
            patch("agents_system.harness.factory.build_runtime", build_runtime),
        )

    message = str(exc_info.value)
    assert "'ghost-bot'" in message
    assert channel in message
    build_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_registration_refuses_a_trusted_input_agent_on_whatsapp() -> None:
    """spec: 'A trusted-input agent cannot be bound to WhatsApp' -- naming
    the agent and the runtime id. The generic agent resolves
    untrusted_input=False."""
    from agents_system.agent.spec import Agent

    app = create_test_app(
        agents={"wa-internal": Agent(name="internal-bot")},
        grants={"wa-internal": ["read:session"]},
    )

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(app, _registration_settings(whatsapp_runtime_id="wa-internal"))

    message = str(exc_info.value)
    assert "'internal-bot'" in message
    assert "'wa-internal'" in message
    assert "untrusted_input" in message
    assert not hasattr(app.state, "runtimes")


@pytest.mark.asyncio
async def test_registration_binds_an_untrusted_input_agent_to_whatsapp() -> None:
    """spec: 'An untrusted-input agent binds successfully' -- a custom agent
    extending sales-agent inherits untrusted_input: true."""
    from agents_system.agent.spec import Agent

    app = create_test_app(
        agents={"wa-support": Agent(name="support-bot", extends="sales-agent")},
        grants={"wa-support": _SALES_GRANT},
    )
    worker = _mock_worker()

    await _boot(
        app,
        _registration_settings(whatsapp_runtime_id="wa-support"),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker",
            return_value=worker,
        ),
    )

    assert app.state.runtimes["wa-support"]["runtime"].definition.untrusted_input
    worker.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_registration_enforces_the_lease_budget_for_a_custom_agent() -> None:
    """spec: 'A WhatsApp-bound custom agent with too generous a timeout fails
    boot' -- ValueError naming the runtime id and the computed values. The
    importer safety ceiling already caps a custom agent at the platform
    default, so the definition is faked here to reach this second check."""
    from agents_system.agent.spec import Agent

    fake_definition = MagicMock()
    fake_definition.untrusted_input = True
    fake_definition.execution_limits = {"total_execution_timeout_s": 540}
    app = create_test_app(
        agents={"wa-support": Agent(name="support-bot", extends="sales-agent")},
        grants={"wa-support": _SALES_GRANT},
    )

    with pytest.raises(ValueError) as exc_info:
        await _boot(
            app,
            _registration_settings(whatsapp_runtime_id="wa-support"),
            patch("agents_system.harness.loader.resolve", return_value=fake_definition),
        )

    message = str(exc_info.value)
    assert "'wa-support'" in message
    assert "540" in message
    assert "600" in message


@pytest.mark.parametrize(
    ("grants", "deploy_grants", "source"),
    [
        # grants= passed, but not for this id.
        ({"acme-sales": _SALES_GRANT}, {}, "grants"),
        # No grants=: DEPLOY_GRANTS is keyed by the registered id, and the
        # old `{deployment}__{role}` key is never consulted.
        (None, {"_generic__sales-agent": _SALES_GRANT}, "DEPLOY_GRANTS"),
    ],
)
@pytest.mark.asyncio
async def test_registration_fails_boot_without_a_grant_for_the_id(
    grants: dict[str, tuple[str, ...]] | None,
    deploy_grants: dict[str, tuple[str, ...]],
    source: str,
) -> None:
    """spec: 'A registered id without a DEPLOY_GRANTS entry fails boot' --
    nothing is granted automatically, whichever source the grants come from."""
    agents = {"acme-sales": "sales-agent", "support-bot": "sales-agent"}
    if grants is None:
        agents = {"support-bot": "sales-agent"}
    app = create_test_app(agents=agents, grants=grants)

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(app, _registration_settings(deploy_grants=deploy_grants))

    message = str(exc_info.value)
    assert "'support-bot'" in message
    assert source in message
    assert not hasattr(app.state, "runtimes")


@pytest.mark.asyncio
async def test_registration_falls_back_to_deploy_grants_keyed_by_the_id() -> None:
    """spec: 'An id migrated from the old key format resolves correctly' --
    with no grants=, DEPLOY_GRANTS keyed by the registered id is the grant."""
    app = create_test_app(agents={"support-bot": "sales-agent"})

    await _boot(
        app,
        _registration_settings(deploy_grants={"support-bot": ("read:catalog",)}),
    )

    from agents_system.permissions import permission_registry

    equipped = app.state.runtimes["support-bot"]["runtime"]
    assert equipped.deploy_grant_ceiling == frozenset(
        {permission_registry.resolve("read:catalog")}
    )


@pytest.mark.asyncio
async def test_registration_with_a_client_needs_an_explicit_root_config() -> None:
    """spec: 'A client-override registration with no RootConfig fails boot'
    -- naming the runtime id."""
    app = create_test_app(
        agents={"acme-sales": "sales-agent"},
        grants={"acme-sales": _SALES_GRANT},
        clients={"acme-sales": "client-a"},
    )

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(app, _registration_settings())

    message = str(exc_info.value)
    assert "'acme-sales'" in message
    assert "RootConfig" in message


@pytest.mark.asyncio
async def test_registration_still_applies_r4_at_grant_time() -> None:
    """ADR-003 R4 -- an untrusted_input custom agent granted a T3
    permission fails boot out of build_runtime, exactly like a predefined
    role on the Settings-driven path."""
    from agents_system.agent.spec import Agent

    app = create_test_app(
        agents={"support-bot": Agent(name="support-bot", extends="sales-agent")},
        grants={"support-bot": ["exec:command"]},
    )

    with pytest.raises(UntrustedInputGrantError):
        await _boot(app, _registration_settings())


# ---------------------------------------------------------------------------
# ADR-004 PR4b — the Settings-driven boot reads AGENT_REGISTRATIONS
#
# Without `create_app(agents=...)`, the lifespan builds its registrations
# from AGENT_REGISTRATIONS ({id: "role" | "role@client"}) and runs them
# through the same loop. ADAPTER_RUNTIMES/WHATSAPP_RUNTIME_ID/DEPLOY_GRANTS
# name those ids as opaque keys: the `{deployment}__{role}` scheme is gone.
# Every test in this section has "env_registration" in its name.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_registration_serves_a_role_without_a_sentinel_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec: 'A runtime id needs no _generic sentinel to mean "no
    deployment"' -- a value with no `@client` is the role alone."""
    app = create_test_app()

    await _boot(
        app,
        _env_settings(
            monkeypatch,
            AGENT_REGISTRATIONS='{"support-bot": "sales-agent"}',
            ADAPTER_RUNTIMES='["support-bot"]',
            DEPLOY_GRANTS='{"support-bot": ["read:catalog"]}',
        ),
    )

    assert set(app.state.runtimes) == {"support-bot"}
    definition = app.state.runtimes["support-bot"]["runtime"].definition
    assert definition.role_name == "sales-agent"
    assert definition.deployment is None
    assert app.state.adapter_model_ids == frozenset({"support-bot"})


@pytest.mark.asyncio
async def test_env_registration_treats_a_legacy_shaped_id_as_opaque(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec: 'A legacy-shaped id string is treated as opaque, not parsed' --
    `acme__sales-agent` is one key; it serves the role its registration
    names (operator-agent), with no client."""
    app = create_test_app()

    await _boot(
        app,
        _env_settings(
            monkeypatch,
            AGENT_REGISTRATIONS='{"acme__sales-agent": "operator-agent"}',
            ADAPTER_RUNTIMES='["acme__sales-agent"]',
            DEPLOY_GRANTS='{"acme__sales-agent": ["read:session"]}',
        ),
    )

    definition = app.state.runtimes["acme__sales-agent"]["runtime"].definition
    assert definition.role_name == "operator-agent"
    assert definition.deployment is None


@pytest.mark.asyncio
async def test_env_registration_builds_every_entry_and_publishes_only_adapter_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Like `agents=`, every AGENT_REGISTRATIONS entry is built at boot;
    /v1 publishes only the ADAPTER_RUNTIMES ids, and WhatsApp binds its own."""
    app = create_test_app()
    worker = _mock_worker()

    await _boot(
        app,
        _env_settings(
            monkeypatch,
            AGENT_REGISTRATIONS=(
                '{"acme-sales": "sales-agent", "wa-sales": "sales-agent",'
                ' "back-office": "operator-agent"}'
            ),
            ADAPTER_RUNTIMES='["acme-sales"]',
            WHATSAPP_RUNTIME_ID="wa-sales",
            DEPLOY_GRANTS=(
                '{"acme-sales": ["read:catalog"], "wa-sales": ["read:catalog"],'
                ' "back-office": ["read:session"]}'
            ),
        ),
        patch(
            "agents_system.services.webhook_worker.DeferredWebhookWorker",
            return_value=worker,
        ),
    )

    assert set(app.state.runtimes) == {"acme-sales", "wa-sales", "back-office"}
    assert app.state.adapter_model_ids == frozenset({"acme-sales"})
    worker.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_env_registration_grant_is_keyed_by_the_new_id_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec: 'An id migrated from the old key format resolves correctly' --
    the old `_generic__sales-agent` DEPLOY_GRANTS key is never consulted for
    the runtime now registered as `acme-sales`."""
    app = create_test_app()

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(
            app,
            _env_settings(
                monkeypatch,
                AGENT_REGISTRATIONS='{"acme-sales": "sales-agent"}',
                DEPLOY_GRANTS='{"_generic__sales-agent": ["read:catalog"]}',
            ),
        )

    message = str(exc_info.value)
    assert "'acme-sales'" in message
    assert "DEPLOY_GRANTS" in message
    assert not hasattr(app.state, "runtimes")


@pytest.mark.parametrize(
    ("registrations", "named"),
    [
        ('{"acme-sales": "sales-agent@client-a@x"}', "'sales-agent@client-a@x'"),
        ('{"acme-sales": "sales-agent@"}', "'sales-agent@'"),
        ('{"acme-sales": "../sales-agent"}', "'../sales-agent'"),
        # The removed sentinel is not a valid runtime id either.
        ('{"_generic__sales-agent": "sales-agent"}', "'_generic__sales-agent'"),
        ('{"bad id": "sales-agent"}', "'bad id'"),
    ],
)
@pytest.mark.asyncio
async def test_env_registration_fails_boot_on_a_malformed_entry(
    monkeypatch: pytest.MonkeyPatch, registrations: str, named: str
) -> None:
    """A malformed AGENT_REGISTRATIONS id or value fails boot, naming it and
    the variable, before any runtime is built -- never logged and skipped."""
    app = create_test_app(roots=RootConfig(deployments_root=_FIXTURE_DEPLOYMENTS))
    build_runtime = MagicMock(side_effect=AssertionError("must not be reached"))

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(
            app,
            _env_settings(
                monkeypatch,
                AGENT_REGISTRATIONS=registrations,
                DEPLOY_GRANTS='{"acme-sales": ["read:catalog"]}',
            ),
            patch("agents_system.harness.factory.build_runtime", build_runtime),
        )

    message = str(exc_info.value)
    assert "AGENT_REGISTRATIONS" in message
    assert named in message
    build_runtime.assert_not_called()
    assert not hasattr(app.state, "runtimes")


@pytest.mark.parametrize(
    ("channel", "env"),
    [
        ("ADAPTER_RUNTIMES", {"ADAPTER_RUNTIMES": '["_generic__sales-agent"]'}),
        ("WHATSAPP_RUNTIME_ID", {"WHATSAPP_RUNTIME_ID": "acme__sales-agent"}),
    ],
)
@pytest.mark.asyncio
async def test_env_registration_fails_boot_for_an_unmigrated_channel_id(
    monkeypatch: pytest.MonkeyPatch, channel: str, env: dict[str, str]
) -> None:
    """A deployment still configured with `{deployment}__{role}` ids and no
    AGENT_REGISTRATIONS fails boot naming the id, the channel and the
    variable to migrate to -- the id is never parsed into a role."""
    app = create_test_app()
    build_runtime = MagicMock(side_effect=AssertionError("must not be reached"))

    with pytest.raises(DefinitionError) as exc_info:
        await _boot(
            app,
            _env_settings(monkeypatch, **env),
            patch("agents_system.harness.factory.build_runtime", build_runtime),
        )

    message = str(exc_info.value)
    (runtime_id,) = (
        [env["WHATSAPP_RUNTIME_ID"]]
        if channel == "WHATSAPP_RUNTIME_ID"
        else ["_generic__sales-agent"]
    )
    assert repr(runtime_id) in message
    assert channel in message
    assert "AGENT_REGISTRATIONS" in message
    build_runtime.assert_not_called()


@pytest.mark.asyncio
async def test_env_registration_one_unresolvable_entry_blocks_the_whole_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """spec: 'One bad registration entry blocks the whole boot' -- on the
    Settings-driven path too: a role that does not exist fails boot, and the
    valid entry is not served."""
    app = create_test_app()

    with pytest.raises(DefinitionError, match="no-such-agent"):
        await _boot(
            app,
            _env_settings(
                monkeypatch,
                AGENT_REGISTRATIONS=(
                    '{"acme-sales": "sales-agent", "ghost": "no-such-agent"}'
                ),
                DEPLOY_GRANTS=(
                    '{"acme-sales": ["read:catalog"], "ghost": ["read:catalog"]}'
                ),
            ),
        )

    assert not hasattr(app.state, "runtimes")


@pytest.mark.asyncio
async def test_env_registration_is_ignored_when_create_app_gets_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`create_app(agents=...)` is the whole registration: AGENT_REGISTRATIONS
    is the fallback for when it is absent, never merged into it."""
    app = create_test_app(
        agents={"acme-sales": "sales-agent"}, grants={"acme-sales": _SALES_GRANT}
    )

    await _boot(
        app,
        _env_settings(
            monkeypatch, AGENT_REGISTRATIONS='{"back-office": "operator-agent"}'
        ),
    )

    assert set(app.state.runtimes) == {"acme-sales"}


@pytest.mark.asyncio
async def test_env_registration_leaves_demo_build_app_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """design.md D5: `examples/demo/app.py`'s `build_app` (moved from
    `agents_system.demo` by PR5, ADR-004) passes no `agents=` and needs no
    change -- with nothing registered in the environment it boots with an
    empty runtime cache."""
    from _demo_app import demo_app

    app = demo_app.build_app(engine=MagicMock(), model=MagicMock())

    await _boot(app, _env_settings(monkeypatch))

    assert app.state.runtimes == {}
    assert app.state.adapter_model_ids == frozenset()
