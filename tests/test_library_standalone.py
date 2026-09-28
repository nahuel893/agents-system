"""Issue #166 -- the library runs a turn in-process without the server.

`agents_system` is an importable library: `build_runtime` -> `AgentRuntime`
-> `run_turn`, driven by the importer's own code, with no `create_app` and no
server environment. Two things used to break that path:

1. `run_turn` priced the turn through `get_settings()`, which constructs the
   server `Settings` -- and its fail-closed validation raises without
   `META_WEBHOOK_SECRET`, AFTER the model and tools had already run.
2. With no `AuditSink` registered, every audit emission logged a full
   traceback (`audit.emit_failed` with `exc_info`).

The served app is the contrast: `create_app`'s lifespan still validates
`Settings` fail-closed, registers a real `AuditSink`, and prices turns from
`MODEL_PRICES`. Those tests live at the bottom of this file.
"""

from __future__ import annotations

import asyncio
import contextvars
import os
from collections.abc import Iterator, Sequence
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from agents_system.config import Settings, get_settings

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """A scripted chat model whose `bind_tools` does not raise -- the shape
    every offline runtime test uses (`AgentRuntime.__init__` binds tools)."""

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> ToolCallingFakeModel:
        return self


def _tool_call(
    name: str, args: dict[str, Any], usage: dict[str, int] | None = None
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": "call-1", "name": name, "args": args, "type": "tool_call"}],
        usage_metadata=usage,  # type: ignore[arg-type]
    )


def _one_tool_then_answer() -> ToolCallingFakeModel:
    """One `catalog_search` call, then a final answer. The two model calls
    report 1M input + 0.5M output tokens between them."""
    return ToolCallingFakeModel(
        responses=[
            _tool_call(
                "catalog_search",
                {"q": "Item Alpha"},
                usage={
                    "input_tokens": 400_000,
                    "output_tokens": 100_000,
                    "total_tokens": 500_000,
                },
            ),
            AIMessage(
                content="Yes, Item Alpha is in stock.",
                usage_metadata={
                    "input_tokens": 600_000,
                    "output_tokens": 400_000,
                    "total_tokens": 1_000_000,
                },
            ),
        ]
    )


def _clear_server_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every variable the server `Settings` reads, plus the suite's
    own `ALLOW_INSECURE=true` (tests/conftest.py), so constructing `Settings`
    fails closed exactly as it does for an importer with no server env.
    `.env` is already out of the picture: the autouse
    `_isolate_settings_from_developer_env` fixture disables `env_file`."""
    server_fields = set(Settings.model_fields)
    for name in list(os.environ):
        if name.lower() in server_fields:
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def server_env_absent(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """No server environment, and a record of every `Settings` construction.

    The spy delegates to the real `__init__`, so a construction still fails
    with the real validation error -- the list only makes a construction
    visible even if some caller were to swallow that error.
    """
    _clear_server_environment(monkeypatch)
    get_settings.cache_clear()
    with pytest.raises(ValidationError, match="meta_webhook_secret"):
        Settings()

    constructions: list[str] = []
    real_init = Settings.__init__

    def spying_init(self: Settings, *args: Any, **kwargs: Any) -> None:
        constructions.append("Settings()")
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(Settings, "__init__", spying_init)
    try:
        yield constructions
    finally:
        get_settings.cache_clear()


@pytest.fixture
def no_audit_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    """No `AuditSink` registered, and the once-per-process warning re-armed."""
    from agents_system.audit import sink as sink_module
    from agents_system.harness import injector

    monkeypatch.setattr(sink_module, "_registered_sink", None)
    monkeypatch.setattr(injector, "_warned_no_audit_sink", False)


async def _settle_audit_emits() -> None:
    """Wait for every fire-and-forget audit emission scheduled so far."""
    from agents_system.harness import injector

    while injector._pending_emits:
        await asyncio.gather(*tuple(injector._pending_emits))


def _registry_with_recording_catalog(calls: list[dict[str, Any]]) -> Any:
    from conftest import build_test_registry

    def catalog_search(inputs: dict[str, Any], /) -> dict[str, Any]:
        calls.append(dict(inputs))
        return {"results": [{"id": "item-001", "name": "Item Alpha", "stock": 50}]}

    return build_test_registry(catalog_connector=catalog_search)


# ---------------------------------------------------------------------------
# The library path: build_runtime -> AgentRuntime -> run_turn, no server env
# ---------------------------------------------------------------------------


async def test_a_full_turn_runs_in_process_without_any_server_environment(
    server_env_absent: list[str],
) -> None:
    """Acceptance criterion 1: a tool call and a final answer, in-process,
    with `.env` absent and every server variable cleared -- and the server
    `Settings` never constructed along the way."""
    import agents_system

    calls: list[dict[str, Any]] = []
    equipped = agents_system.build_runtime(
        "sales-agent",
        _registry_with_recording_catalog(calls),
        granted_permissions=["read:catalog"],
    )
    agent = agents_system.AgentRuntime(equipped, model=_one_tool_then_answer())

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="Do you have Item Alpha in stock?")],
        session_id="library-session",
    )

    assert calls == [{"q": "Item Alpha"}]
    tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    assert "Item Alpha" in str(tool_messages[0].content)
    assert result.messages[-1].content == "Yes, Item Alpha is in stock."
    # No prices were given, so the cost is honestly unknown -- never guessed.
    assert result.usage.total_tokens == 1_500_000
    assert result.usage.cost_usd is None
    assert server_env_absent == [], "the library path constructed Settings"


async def test_model_prices_are_injected_explicitly_not_read_from_settings(
    server_env_absent: list[str],
) -> None:
    """Prices come in as an `AgentRuntime` argument -- the explicit
    counterpart of `MODEL_PRICES`, which `create_app` fills in for the
    served app. Keyed by the provider model id, as before."""
    import agents_system
    from agents_system.agent.graph import model_display_name

    model = _one_tool_then_answer()
    equipped = agents_system.build_runtime(
        "sales-agent",
        _registry_with_recording_catalog([]),
        granted_permissions=["read:catalog"],
    )
    agent = agents_system.AgentRuntime(
        equipped,
        model=model,
        model_prices={
            model_display_name(model): agents_system.ModelPrice(
                input_per_million=1.0, output_per_million=2.0
            )
        },
    )

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="Do you have Item Alpha in stock?")],
        session_id="library-session",
    )

    # 1M input @ $1/M + 0.5M output @ $2/M
    assert result.usage.cost_usd == pytest.approx(2.0)
    assert server_env_absent == []


async def test_without_an_audit_sink_one_warning_per_process_and_no_traceback(
    no_audit_sink: None,
) -> None:
    """Acceptance criterion 2 (library half): no registered sink means one
    structured warning for the whole process -- not a traceback per event."""
    import agents_system

    with structlog.testing.capture_logs() as logs:
        for session in ("first", "second"):
            equipped = agents_system.build_runtime(
                "sales-agent",
                _registry_with_recording_catalog([]),
                granted_permissions=["read:catalog"],
            )
            agent = agents_system.AgentRuntime(equipped, model=_one_tool_then_answer())
            await agent.run_turn(
                [HumanMessage(content="Do you have Item Alpha in stock?")],
                session_id=session,
            )
        await _settle_audit_emits()

    with_traceback = [entry for entry in logs if entry.get("exc_info")]
    assert with_traceback == [], with_traceback
    assert not [e for e in logs if e["event"] == "audit.emit_failed"]
    warnings = [e for e in logs if e["event"] == "audit.sink_not_registered"]
    assert len(warnings) == 1, logs
    assert warnings[0]["log_level"] == "warning"


async def test_a_sink_registered_through_the_public_api_receives_the_events(
    no_audit_sink: None,
) -> None:
    """The documented way for a library user: `agents_system.AuditSink`,
    started, then registered with `AuditSink.set_current`. Once registered,
    the turn's events reach it and the no-sink warning never fires."""
    import agents_system

    recorded: list[Any] = []

    class InMemorySink(agents_system.AuditSink):
        async def _flush_batch(self, batch: list[Any]) -> None:
            recorded.extend(batch)

    sink = InMemorySink(session_factory=MagicMock())
    await sink.start()
    agents_system.AuditSink.set_current(sink)
    try:
        with structlog.testing.capture_logs() as logs:
            equipped = agents_system.build_runtime(
                "sales-agent",
                _registry_with_recording_catalog([]),
                granted_permissions=["read:catalog"],
            )
            agent = agents_system.AgentRuntime(equipped, model=_one_tool_then_answer())
            await agent.run_turn(
                [HumanMessage(content="Do you have Item Alpha in stock?")],
                session_id="audited",
            )
            await _settle_audit_emits()
    finally:
        await sink.stop()
        agents_system.AuditSink.clear_current(sink)

    event_types = {getattr(event, "event_type", None) for event in recorded}
    assert {"runtime_built", "runtime_initialized", "tool_call_attempted"} <= (
        event_types
    )
    assert not [e for e in logs if e["event"] == "audit.sink_not_registered"]
    assert agents_system.AuditSink.registered() is None


# ---------------------------------------------------------------------------
# The served app: unchanged -- fail-closed Settings, a real AuditSink,
# MODEL_PRICES cost
# ---------------------------------------------------------------------------


class ServedFakeModel(ToolCallingFakeModel):
    """Its class name is its derived price key (`model_display_name`)."""


def _served_environment(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    """The environment an operator sets for the served app -- through the
    real `get_settings()`, never an injected `Settings`."""
    _clear_server_environment(monkeypatch)
    env = {
        "META_WEBHOOK_SECRET": "served-app-secret",
        "ADAPTER_API_KEY": "served-app-key",
        "AGENT_REGISTRATIONS": '{"sales": "sales-agent"}',
        "ADAPTER_RUNTIMES": '["sales"]',
        "DEPLOY_GRANTS": '{"sales": ["read:catalog"]}',
        "WHATSAPP_CHECKPOINTER_ENABLED": "false",
        "MODEL_PRICES": (
            '{"ServedFakeModel": {"input_per_million": 2.0, "output_per_million": 4.0}}'
        ),
    }
    env.update(extra)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def _served_app(model: Any) -> Any:
    from conftest import create_test_app

    return create_test_app(chat_model=model)


def _served_patches() -> tuple[Any, ...]:
    engine = MagicMock()
    engine.dispose = AsyncMock()
    return (
        patch("agents_system.main.get_engine", return_value=engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
    )


@pytest.fixture
def served_env_cleanup() -> Iterator[None]:
    """Undo what booting the served app changes process-wide: the cached
    `Settings`, and the structlog configuration `create_app` installs
    (`setup_logging`), which later tests' rendered-log assertions assume
    is untouched."""
    logging_config = structlog.get_config()
    yield
    get_settings.cache_clear()
    structlog.configure(**logging_config)


async def test_the_served_app_still_fails_closed_without_its_environment(
    monkeypatch: pytest.MonkeyPatch, served_env_cleanup: None
) -> None:
    """The library no longer needs `Settings`; the served app still does,
    and its boot validation still fails closed."""
    from agents_system.main import lifespan

    _clear_server_environment(monkeypatch)
    get_settings.cache_clear()

    # `create_app` reads Settings while wiring routes, before the lifespan
    # ever runs -- either one refusing to start is the fail-closed boot.
    p1, p2, p3 = _served_patches()
    with p1, p2, p3, pytest.raises(ValidationError, match="meta_webhook_secret"):
        app = _served_app(ServedFakeModel(responses=[AIMessage(content="hi")]))
        async with lifespan(app):
            pass


async def test_the_served_app_prices_turns_from_model_prices(
    monkeypatch: pytest.MonkeyPatch, served_env_cleanup: None
) -> None:
    """`MODEL_PRICES` still prices every served turn, keyed by the provider
    model id -- now handed to each runtime by the lifespan."""
    from agents_system.main import lifespan

    _served_environment(monkeypatch)
    model = ServedFakeModel(
        responses=[
            AIMessage(
                content="hi",
                usage_metadata={
                    "input_tokens": 1_000_000,
                    "output_tokens": 500_000,
                    "total_tokens": 1_500_000,
                },
            )
        ]
    )
    app = _served_app(model)

    async def discard_batch(self: Any, batch: list[Any]) -> None:
        """The audit rows are not this test's subject; keep them off the
        mocked engine."""

    from agents_system.audit.sink import AuditSink

    p1, p2, p3 = _served_patches()
    with p1, p2, p3, patch.object(AuditSink, "_flush_batch", discard_batch):
        async with lifespan(app):
            result = await app.state.runtimes["sales"].run_turn_with_usage(
                [HumanMessage(content="hi")], session_id="served"
            )

    # 1M input @ $2/M + 0.5M output @ $4/M
    assert result.usage.cost_usd == pytest.approx(4.0)


async def test_the_served_app_registers_a_real_sink_that_every_request_sees(
    monkeypatch: pytest.MonkeyPatch, served_env_cleanup: None
) -> None:
    """Acceptance criterion 2 (served half): the lifespan registers a real
    `AuditSink`, and a turn run from a request task records into it.

    uvicorn runs the lifespan in its own task and each request in a task
    that does NOT inherit the lifespan's context variables, so the request
    below runs in a fresh `contextvars.Context()` -- the way a /v1 request
    reaches a runtime in production.
    """
    from agents_system.audit.sink import AuditSink
    from agents_system.main import lifespan

    _served_environment(monkeypatch)
    recorded: list[Any] = []

    async def record_batch(self: AuditSink, batch: list[Any]) -> None:
        recorded.extend(batch)

    app = _served_app(
        ServedFakeModel(
            responses=[
                _tool_call("catalog_search", {"q": "Item Alpha"}),
                AIMessage(content="Yes, Item Alpha is in stock."),
            ]
        )
    )

    async def a_request() -> AuditSink | None:
        try:
            registered: AuditSink | None = AuditSink.current()
        except RuntimeError:
            registered = None
        await app.state.runtimes["sales"].run_turn(
            [HumanMessage(content="Do you have Item Alpha in stock?")],
            session_id="served-request",
        )
        await _settle_audit_emits()
        return registered

    p1, p2, p3 = _served_patches()
    with (
        p1,
        p2,
        p3,
        patch.object(AuditSink, "_flush_batch", record_batch),
        structlog.testing.capture_logs() as logs,
    ):
        async with lifespan(app):
            sink = app.state.audit_sink
            assert type(sink) is AuditSink
            loop = asyncio.get_running_loop()
            registered = await loop.create_task(
                a_request(), context=contextvars.Context()
            )
            assert registered is sink, "a request task cannot see the lifespan's sink"
            await sink.drain()

    event_types = {getattr(event, "event_type", None) for event in recorded}
    assert "runtime_built" in event_types
    assert "tool_call_attempted" in event_types
    assert not [e for e in logs if e["event"] == "audit.sink_not_registered"]
