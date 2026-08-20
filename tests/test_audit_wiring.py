"""The audit wiring must actually deliver events, not merely call the emitter.

This file exists because it did not. Every call site in `injector`,
`interceptor`, `factory` and `graph` invoked `_emit_async(...)` bare. That is a
coroutine function: calling it builds a coroutine object and drops it before
the body runs. The whole audit trail emitted nothing, 628 tests passed, and the
only evidence was `RuntimeWarning: coroutine '_emit_async' was never awaited` —
which does not fail a test run.

Every other audit test asserts on the recorder or the sink in isolation, so all
of them stayed green while the path between them was severed. These tests
assert delivery end to end: a synchronous caller emits, and the event arrives.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentsys.audit.sink import AuditSink
from agentsys.harness.injector import resolve_tool_surface
from agentsys.harness.loader import AgentDefinition
from agentsys.harness.registry import ToolRegistry, ToolSpec


class CapturingSink(AuditSink):
    """Real sink, real queue, real drainer — only the DB write is replaced."""

    def __init__(self) -> None:
        super().__init__(session_factory=None, maxsize=100)  # type: ignore[arg-type]
        self.captured: list[Any] = []

    async def _flush_batch(self, batch: list[Any]) -> None:
        self.captured.extend(batch)


def _definition(*, tools: tuple[str, ...], permissions: tuple[str, ...]) -> AgentDefinition:
    return AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment="badie",
        system_prompt="prompt",
        tools=tools,
        skills=(),
        context={},
        permissions=permissions,
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )


def _registry_with(*specs: ToolSpec) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return registry


async def _settle(sink: CapturingSink) -> None:
    """Wait for the event to actually reach ``_flush_batch``.

    Deliberately NOT ``sink.drain()``. The drainer loop dequeues into a private
    batch and only flushes it every 100ms, so ``drain()`` looks at an empty
    queue and reports success while the event is still in flight. Waiting out
    the drainer's own interval is what proves delivery.
    """
    for _ in range(20):
        if sink.captured:
            return
        await asyncio.sleep(0.02)


@pytest.fixture
async def sink() -> Any:
    s = CapturingSink()
    await s.start()
    previous = None
    try:
        previous = AuditSink.current()
    except Exception:
        previous = None
    AuditSink.set_current(s)
    yield s
    await s.stop()
    if previous is not None:
        AuditSink.set_current(previous)


@pytest.mark.asyncio
async def test_a_synchronous_caller_delivers_an_event_to_the_sink(sink: CapturingSink) -> None:
    """resolve_tool_surface is sync. Its audit events must still arrive.

    This is the regression: with a bare `_emit_async(...)` call the coroutine is
    discarded, `captured` stays empty, and nothing anywhere reports it.
    """
    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=lambda **_: "ok",
    )
    resolve_tool_surface(
        _definition(tools=("catalog_search",), permissions=("read:catalog",)),
        _registry_with(spec),
        ["read:catalog"],
    )

    await _settle(sink)

    assert sink.captured, (
        "resolve_tool_surface emitted no audit event. The emit helper is a "
        "coroutine; a bare call discards it before the body runs."
    )


@pytest.mark.asyncio
async def test_a_denied_tool_is_auditable(sink: CapturingSink) -> None:
    """A denial is the event most worth having, so it gets its own guard."""
    spec = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders",),
        connector=lambda **_: "ok",
    )
    result = resolve_tool_surface(
        _definition(tools=("order_writer",), permissions=("write:orders",)),
        _registry_with(spec),
        [],  # the caller holds no grants, so the tool is denied
    )
    assert result.denied, "precondition: the tool must actually be denied"

    await _settle(sink)

    assert sink.captured, "a denied tool produced no audit event"


@pytest.mark.asyncio
async def test_emitting_without_a_running_loop_is_not_an_error() -> None:
    """Sync entry points (CLI, unit tests) have no loop. That must not raise."""
    from agentsys.harness.injector import _emit

    def no_loop_here() -> None:
        _emit("record_skill_loaded", definition=_definition(tools=(), permissions=()), skill="s")

    await asyncio.get_running_loop().run_in_executor(None, no_loop_here)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "AuditSink.stop() sets _shutdown and then immediately cancels the drainer "
        "task. The drainer's own shutdown flush lives after its `while not "
        "self._shutdown` loop, but the cancellation raises CancelledError inside "
        "the awaited queue.get(), so that flush is never reached: every event "
        "still queued or held in the drainer's in-flight batch is discarded. "
        "stop() also never calls drain(), and drain() cannot see the in-flight "
        "batch anyway."
    ),
)
async def test_stop_does_not_lose_queued_events() -> None:
    """Shutdown must not drop the tail of the audit log."""
    sink = CapturingSink()
    await sink.start()
    AuditSink.set_current(sink)

    event = await __import__(
        "agentsys.audit.recorder", fromlist=["record_tool_denied"]
    ).record_tool_denied(
        _definition(tools=(), permissions=()), tool_name="order_writer", reason="denied"
    )
    await sink.record(event)

    await sink.stop()

    assert sink.captured, "stop() discarded an event that was already accepted"
