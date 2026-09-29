"""The checkpointer failure policy (issue #24).

A thread's history is read once at the start of a turn and the turn is saved
once at the end. The policy for a checkpointer that fails is the same for
every backend, because it keys on WHERE the failure happened, never on the
exception class a particular client library raises:

- The read fails: the turn is answered without the thread's history and is
  not saved, so a thread that could not be read is never overwritten by one
  that lacks its earlier turns.
- The save fails: the finished answer is returned and nothing runs again, so
  no model call is repeated and no tool call is replayed.

Either way the failure is logged as ``runtime.checkpointer_degraded`` and the
turn is never failed because of the checkpointer. These tests use
``InMemorySaver`` subclasses and plain exceptions: no Redis and no Postgres.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver

from agents_system.agent.graph import AgentRuntime
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition
from agents_system.harness.registry import Tier, ToolSpec


class _BackendDown(Exception):
    """Stands in for whatever a checkpointer's client library raises."""


class _ToolAwareFakeModel(FakeMessagesListChatModel):
    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _ToolAwareFakeModel:
        return self


class _CountingModel(_ToolAwareFakeModel):
    """Counts provider calls and remembers what the last one received."""

    async def ainvoke(self, input: Any, *args: Any, **kwargs: Any) -> AIMessage:  # type: ignore[override]
        object.__setattr__(self, "calls", getattr(self, "calls", 0) + 1)
        object.__setattr__(self, "seen", list(input))
        return await super().ainvoke(input, *args, **kwargs)


class _Saver(InMemorySaver):
    """A working in-memory saver that can be told to fail reads or writes,
    per thread, and counts the writes it accepted."""

    def __init__(
        self,
        *,
        broken_reads: frozenset[str] = frozenset(),
        broken_writes: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__()
        self.broken_reads = broken_reads
        self.broken_writes = broken_writes
        self.puts: dict[str, int] = {}

    async def aget_tuple(self, config: Any) -> Any:
        if config["configurable"]["thread_id"] in self.broken_reads:
            raise _BackendDown("cannot read this thread (simulated)")
        return await super().aget_tuple(config)

    async def aput(
        self,
        config: Any,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> Any:
        thread_id = config["configurable"]["thread_id"]
        if thread_id in self.broken_writes:
            raise _BackendDown("cannot save this thread (simulated)")
        self.puts[thread_id] = self.puts.get(thread_id, 0) + 1
        return await super().aput(config, checkpoint, metadata, new_versions)


def _definition(execution_limits: dict[str, Any] | None = None) -> AgentDefinition:
    return AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="You are a helpful assistant.",
        tools=(),
        skills=(),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=execution_limits,
    )


def _runtime(
    tools: tuple[ToolSpec, ...] = (),
    execution_limits: dict[str, Any] | None = None,
) -> EquippedRuntime:
    return EquippedRuntime(
        definition=_definition(execution_limits),
        system_prompt="You are a helpful assistant.",
        tools=tools,
        denied_tools=(),
        skills=(),
    )


def _counting_tool() -> tuple[ToolSpec, list[dict[str, Any]]]:
    """A tool with a visible side effect: every execution is recorded."""
    executions: list[dict[str, Any]] = []

    async def place_order(inputs: dict[str, Any], *, session: Any = None) -> dict:
        executions.append(dict(inputs))
        return {"order": len(executions)}

    spec = ToolSpec(
        name="place_order",
        required_permissions=(),
        connector=place_order,
        tier=Tier.T0,
    )
    return spec, executions


def _slow_tool() -> ToolSpec:
    async def slow(inputs: dict[str, Any], *, session: Any = None) -> dict:
        await asyncio.sleep(10)
        return {}

    return ToolSpec(
        name="slow_tool", required_permissions=(), connector=slow, tier=Tier.T0
    )


def _tool_call(name: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": {}, "type": "tool_call"}],
        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
    )


def _answer(text: str) -> AIMessage:
    return AIMessage(
        content=text,
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )


async def _history(saver: InMemorySaver, thread_id: str) -> list[Any]:
    stored = await InMemorySaver.aget_tuple(
        saver, {"configurable": {"thread_id": thread_id}}
    )
    if stored is None:
        return []
    return list(stored.checkpoint["channel_values"].get("messages", []))


def _logged(out: str, key: str, value: object) -> bool:
    """Whether a log line carries ``key`` = ``value``, in either structlog
    rendering the suite runs under (console ``key=value`` or JSON)."""
    rendered = value if isinstance(value, int) else f'"{value}"'
    return f"{key}={value}" in out or f'"{key}": {rendered}' in out


# ---------------------------------------------------------------------------
# The read fails
# ---------------------------------------------------------------------------


async def test_a_thread_that_cannot_be_read_is_answered_without_its_history(
    capsys: pytest.CaptureFixture[str],
) -> None:
    saver = _Saver(broken_reads=frozenset({"+549110000001"}))
    model = _CountingModel(responses=[_answer("Still here.")])
    agent = AgentRuntime(_runtime(), model, checkpointer=saver)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="Hello")],
        session_id="s1",
        permissions=(),
        thread_id="+549110000001",
    )

    assert result.messages[-1].content == "Still here."
    assert [m.content for m in result.messages] == ["Hello", "Still here."]
    # Nothing ran twice, so the usage is exact, not unknown.
    assert model.calls == 1  # type: ignore[attr-defined]
    assert result.usage.model_calls == 1
    assert result.usage.total_tokens == 15
    out = capsys.readouterr().out
    assert "runtime.checkpointer_degraded" in out
    assert _logged(out, "operation", "read")


async def test_a_thread_that_cannot_be_read_is_not_overwritten() -> None:
    saver = _Saver(broken_reads=frozenset({"+549110000002"}))
    agent = AgentRuntime(
        _runtime(), _CountingModel(responses=[_answer("ok")]), checkpointer=saver
    )

    await agent.run_turn(
        [HumanMessage(content="Hello")],
        session_id="s1",
        permissions=(),
        thread_id="+549110000002",
    )

    assert saver.puts.get("+549110000002", 0) == 0


# ---------------------------------------------------------------------------
# The save fails
# ---------------------------------------------------------------------------


async def test_a_failed_save_returns_the_answer_without_running_anything_again(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec, executions = _counting_tool()
    saver = _Saver(broken_writes=frozenset({"+549110000003"}))
    model = _CountingModel(
        responses=[_tool_call("place_order", "call_1"), _answer("Order placed.")]
    )
    agent = AgentRuntime(_runtime((spec,)), model, checkpointer=saver)

    result = await agent.run_turn_with_usage(
        [HumanMessage(content="Two boxes, please")],
        session_id="s1",
        permissions=(),
        thread_id="+549110000003",
    )

    assert result.messages[-1].content == "Order placed."
    # The tool's side effect happened once and the model was asked twice
    # (tool call, then answer) -- never a replay of the whole turn.
    assert len(executions) == 1
    assert model.calls == 2  # type: ignore[attr-defined]
    assert result.usage.model_calls == 2
    assert result.usage.total_tokens == 135
    out = capsys.readouterr().out
    assert "runtime.checkpointer_degraded" in out
    assert _logged(out, "operation", "write")


async def test_a_failed_save_leaves_the_thread_at_its_previous_turn() -> None:
    thread = "+549110000004"
    saver = _Saver()
    agent = AgentRuntime(
        _runtime(),
        _CountingModel(responses=[_answer("First."), _answer("Second.")]),
        checkpointer=saver,
    )
    await agent.run_turn(
        [HumanMessage(content="One")], session_id="s1", permissions=(), thread_id=thread
    )
    saver.broken_writes = frozenset({thread})

    await agent.run_turn(
        [HumanMessage(content="Two")], session_id="s1", permissions=(), thread_id=thread
    )

    assert [m.content for m in await _history(saver, thread)] == ["One", "First."]


# ---------------------------------------------------------------------------
# One broken thread never fails every turn
# ---------------------------------------------------------------------------


async def test_one_broken_thread_keeps_answering_and_never_touches_the_others() -> None:
    broken, healthy = "+549110000005", "+549110000006"
    saver = _Saver(broken_reads=frozenset({broken}))
    model = _CountingModel(
        responses=[
            _answer("b1"),
            _answer("h1"),
            _answer("b2"),
            _answer("h2"),
            _answer("b3"),
        ]
    )
    agent = AgentRuntime(_runtime(), model, checkpointer=saver)

    replies = []
    for thread, text in [
        (broken, "B1"),
        (healthy, "H1"),
        (broken, "B2"),
        (healthy, "H2"),
        (broken, "B3"),
    ]:
        result = await agent.run_turn(
            [HumanMessage(content=text)],
            session_id="s1",
            permissions=(),
            thread_id=thread,
        )
        replies.append(result[-1].content)

    assert replies == ["b1", "h1", "b2", "h2", "b3"]
    assert [m.content for m in await _history(saver, healthy)] == [
        "H1",
        "h1",
        "H2",
        "h2",
    ]


# ---------------------------------------------------------------------------
# A turn is saved as one unit
# ---------------------------------------------------------------------------


async def test_a_turn_is_saved_once_however_many_steps_it_takes() -> None:
    spec, _ = _counting_tool()
    thread = "+549110000007"
    saver = _Saver()
    agent = AgentRuntime(
        _runtime((spec,)),
        _CountingModel(
            responses=[
                _tool_call("place_order", "call_a"),
                _tool_call("place_order", "call_b"),
                _answer("Done."),
            ]
        ),
        checkpointer=saver,
    )

    await agent.run_turn(
        [HumanMessage(content="Order")],
        session_id="s1",
        permissions=(),
        thread_id=thread,
    )

    # One save for the whole turn, so a crash in the middle of a turn leaves
    # the thread exactly where the previous turn ended.
    assert saver.puts[thread] == 1
    assert [type(m).__name__ for m in await _history(saver, thread)] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]


async def test_a_timed_out_turn_is_saved_as_the_user_received_it() -> None:
    thread = "+549110000008"
    saver = _Saver()
    agent = AgentRuntime(
        _runtime((_slow_tool(),), {"total_execution_timeout_s": 0.3}),
        _CountingModel(responses=[_tool_call("slow_tool", "call_slow")]),
        checkpointer=saver,
    )

    result = await asyncio.wait_for(
        agent.run_turn_with_usage(
            [HumanMessage(content="Slow one")],
            session_id="s1",
            permissions=(),
            thread_id=thread,
        ),
        timeout=3.0,
    )

    assert result.timed_out is True
    saved = await _history(saver, thread)
    # The interrupted tool call is answered and the timeout reply the user
    # was sent is part of the thread, so the next turn continues from the
    # same conversation the user saw.
    assert [type(m).__name__ for m in saved] == [
        "HumanMessage",
        "AIMessage",
        "ToolMessage",
        "AIMessage",
    ]
    assert isinstance(saved[2], ToolMessage)
    assert saved[2].status == "error"
    assert saved[-1].content == result.messages[-1].content


async def test_a_turn_without_a_thread_id_never_reads_or_writes_the_checkpointer() -> (
    None
):
    saver = _Saver(broken_reads=frozenset({"any"}), broken_writes=frozenset({"any"}))
    agent = AgentRuntime(
        _runtime(), _CountingModel(responses=[_answer("ok")]), checkpointer=saver
    )

    result = await agent.run_turn(
        [HumanMessage(content="Hi")], session_id="s1", permissions=()
    )

    assert result[-1].content == "ok"
    assert saver.puts == {}


async def test_a_thread_that_decodes_into_something_else_is_an_unreadable_thread(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A damaged checkpoint can deserialize without raising, into a value that
    is not a message list (found against real Postgres: a corrupted msgpack
    blob decoded as the integer 0). That is a failed read, not a crash."""

    class _Garbled(_Saver):
        async def aget_tuple(self, config: Any) -> Any:
            found = await super().aget_tuple(config)
            if found is not None:
                found.checkpoint["channel_values"]["messages"] = 0
            return found

    thread = "+549110000009"
    saver = _Garbled()
    agent = AgentRuntime(
        _runtime(),
        _CountingModel(responses=[_answer("first"), _answer("second")]),
        checkpointer=saver,
    )
    await agent.run_turn(
        [HumanMessage(content="One")], session_id="s1", permissions=(), thread_id=thread
    )

    result = await agent.run_turn(
        [HumanMessage(content="Two")], session_id="s1", permissions=(), thread_id=thread
    )

    assert [m.content for m in result] == ["Two", "second"]
    assert _logged(capsys.readouterr().out, "operation", "read")
