"""#289 -- a tool that raises reaches the model as an error result.

Before: ``_execute_tools`` recorded the ``"error"`` outcome and re-raised, so a
single bad tool call (an empty required argument, say) aborted the whole turn
and the model never saw why. Now an ``Exception`` from a tool becomes an error
``ToolMessage`` the model can react to, a per-turn cap (``max_tool_errors``)
bounds the retrying, and the exceptions that carry a security or permission
decision still abort the turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from prometheus_client import CollectorRegistry

from agents_system.agent.graph import AgentRuntime
from agents_system.agent.tool_errors import (
    MAX_ERROR_MESSAGE_CHARS,
    aborting_exceptions,
    tool_error_text,
)
from agents_system.execution_limits import (
    DEFAULT_TOOL_ERROR_REPLY,
    validate_execution_limits,
    validate_tool_error_reply,
)
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition
from agents_system.harness.registry import Tier, ToolSpec
from agents_system.observability.metrics import build_metrics
from agents_system.permissions.errors import UnknownPermissionNameError
from agents_system.proactive import (
    ProactivePermissionError,
    RecipientNotAuthorizedError,
)


class _ScriptedModel(FakeMessagesListChatModel):
    """Answers from its script; counts the model calls it served."""

    calls: int = 0

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _ScriptedModel:
        return self

    async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        object.__setattr__(self, "calls", self.calls + 1)
        return await super().ainvoke(*args, **kwargs)


def _call(name: str = "flaky", call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"id": call_id, "name": name, "args": {}, "type": "tool_call"}],
    )


def _definition(limits: Mapping[str, Any] | None = None) -> AgentDefinition:
    return AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="You are a helpful assistant.",
        tools=(),
        skills=(),
        context={},
        permissions=("read:catalog",),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=limits,
    )


def _spec(connector: Callable[..., Any], name: str = "flaky") -> ToolSpec:
    return ToolSpec(
        name=name,
        required_permissions=("read:catalog",),
        connector=connector,
        tier=Tier.T1,
        description="A tool that may fail",
        input_schema={"type": "object", "properties": {}},
    )


def _runtime(
    connector: Callable[..., Any],
    script: list[AIMessage],
    *,
    limits: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[AgentRuntime, _ScriptedModel, Any]:
    metrics = build_metrics(CollectorRegistry())
    model = _ScriptedModel(responses=script)
    equipped = EquippedRuntime(
        definition=_definition(limits),
        system_prompt="You are a helpful assistant.",
        tools=(_spec(connector),),
        denied_tools=(),
        skills=(),
    )
    runtime = AgentRuntime(equipped, model, metrics=metrics, **kwargs)
    return runtime, model, metrics


async def _turn(runtime: AgentRuntime) -> list[Any]:
    return await runtime.run_turn([HumanMessage(content="go")], session_id="s1")


def _raising(exc: BaseException) -> Callable[[dict[str, Any]], Any]:
    def connector(inputs: dict[str, Any]) -> Any:
        raise exc

    return connector


def _tool_messages(messages: list[Any]) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


# ---------------------------------------------------------------------------
# A raising tool reaches the model as an error result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_raising_tool_yields_an_error_tool_message_and_the_turn_completes() -> (
    None
):
    runtime, model, metrics = _runtime(
        _raising(ValueError("codigo must be a non-empty string")),
        [_call(), AIMessage(content="Let me retry with a shorter script.")],
    )

    messages = await _turn(runtime)

    (tool_message,) = _tool_messages(messages)
    assert tool_message.status == "error"
    assert tool_message.tool_call_id == "c1"
    assert "flaky" in tool_message.content
    assert "ValueError" in tool_message.content
    assert "codigo must be a non-empty string" in tool_message.content
    # The model got another step and answered.
    assert model.calls == 2
    assert messages[-1].content == "Let me retry with a shorter script."
    # The failed call is still counted as an "error" outcome.
    assert (
        metrics.tool_calls_total.labels(tool="flaky", outcome="error")._value.get() == 1
    )


@pytest.mark.asyncio
async def test_an_async_connector_that_raises_is_handled_the_same_way() -> None:
    async def connector(inputs: dict[str, Any], *, session: Any) -> Any:
        raise TypeError("unexpected argument")

    runtime, _, _ = _runtime(connector, [_call(), AIMessage(content="ok")])

    messages = await _turn(runtime)

    (tool_message,) = _tool_messages(messages)
    assert tool_message.status == "error"
    assert "TypeError" in tool_message.content
    assert messages[-1].content == "ok"


# ---------------------------------------------------------------------------
# The text the model receives is short and carries no traceback
# ---------------------------------------------------------------------------


def test_the_error_text_is_one_line_short_and_has_no_traceback() -> None:
    try:
        raise ValueError("bad\nvalue\twith   spaces " + "x" * 5000)
    except ValueError as error:
        text = tool_error_text("flaky", error)

    assert "\n" not in text
    assert "\t" not in text
    assert "Traceback" not in text
    assert 'File "' not in text
    assert text.startswith("Tool 'flaky' failed: ValueError: bad value with spaces")
    assert text.endswith("...")
    assert len(text) < MAX_ERROR_MESSAGE_CHARS + 100


def test_the_error_text_without_a_message_names_only_the_class() -> None:
    assert tool_error_text("flaky", KeyError()) == "Tool 'flaky' failed: KeyError"


@pytest.mark.asyncio
async def test_the_message_the_model_receives_has_no_traceback() -> None:
    def connector(inputs: dict[str, Any]) -> Any:
        def inner() -> None:
            raise RuntimeError("boom")

        inner()

    runtime, _, _ = _runtime(connector, [_call(), AIMessage(content="ok")])

    messages = await _turn(runtime)

    (tool_message,) = _tool_messages(messages)
    assert tool_message.content == "Tool 'flaky' failed: RuntimeError: boom"


# ---------------------------------------------------------------------------
# A cap on tool errors per turn ends the turn with a controlled reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_tool_errors_stop_at_the_default_cap_with_the_controlled_reply() -> (
    None
):
    script = [_call(call_id=f"c{n}") for n in range(10)]
    runtime, model, metrics = _runtime(_raising(ValueError("nope")), script)

    messages = await _turn(runtime)

    assert len(_tool_messages(messages)) == 3
    assert model.calls == 3
    assert isinstance(messages[-1], AIMessage)
    assert messages[-1].content == DEFAULT_TOOL_ERROR_REPLY
    assert not messages[-1].tool_calls
    assert metrics.limit_trips_total.labels(limit="max_tool_errors")._value.get() == 1


@pytest.mark.asyncio
async def test_the_cap_and_the_reply_are_configurable_per_runtime() -> None:
    script = [_call(call_id=f"c{n}") for n in range(10)]
    runtime, model, _ = _runtime(
        _raising(ValueError("nope")),
        script,
        execution_limits={"max_tool_errors": 1},
        tool_error_reply="Algo salio mal {user}.",
    )

    messages = await _turn(runtime)

    assert len(_tool_messages(messages)) == 1
    assert model.calls == 1
    # Plain text, sent as given: never templated.
    assert messages[-1].content == "Algo salio mal {user}."


@pytest.mark.asyncio
async def test_a_definition_may_tighten_the_cap_but_not_exceed_the_deployments() -> (
    None
):
    script = [_call(call_id=f"c{n}") for n in range(10)]
    runtime, _, _ = _runtime(
        _raising(ValueError("nope")),
        script,
        limits={"max_tool_errors": 9},
        execution_limits={"max_tool_errors": 2},
    )

    assert runtime.execution_limits["max_tool_errors"] == 2
    messages = await _turn(runtime)
    assert len(_tool_messages(messages)) == 2


@pytest.mark.asyncio
async def test_a_tool_timeout_counts_toward_the_cap() -> None:
    script = [_call(call_id=f"c{n}") for n in range(10)]
    runtime, model, _ = _runtime(
        _raising(TimeoutError()), script, execution_limits={"max_tool_errors": 2}
    )

    messages = await _turn(runtime)

    assert len(_tool_messages(messages)) == 2
    assert model.calls == 2
    assert messages[-1].content == DEFAULT_TOOL_ERROR_REPLY


@pytest.mark.asyncio
async def test_successful_calls_do_not_count_toward_the_cap() -> None:
    state = {"n": 0}

    def connector(inputs: dict[str, Any]) -> Any:
        state["n"] += 1
        if state["n"] % 2:
            raise ValueError("odd call")
        return {"ok": True}

    script = [_call(call_id=f"c{n}") for n in range(4)] + [AIMessage(content="done")]
    runtime, _, _ = _runtime(connector, script)

    messages = await _turn(runtime)

    # Two errors (calls 1 and 3), under the cap of three.
    assert messages[-1].content == "done"


# ---------------------------------------------------------------------------
# Security-relevant exceptions still abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        UnknownPermissionNameError(name="read:nothing"),
        ProactivePermissionError("no send:proactive"),
        RecipientNotAuthorizedError("not in the directory"),
    ],
)
async def test_permission_decisions_still_abort_the_turn(exc: Exception) -> None:
    runtime, model, metrics = _runtime(
        _raising(exc), [_call(), AIMessage(content="unreachable")]
    )

    with pytest.raises(type(exc)):
        await _turn(runtime)

    assert model.calls == 1
    # Aborting still records the call as an "error" outcome.
    assert (
        metrics.tool_calls_total.labels(tool="flaky", outcome="error")._value.get() == 1
    )


def test_the_abort_set_names_the_guard_and_sandbox_exceptions_when_present() -> None:
    names = {cls.__name__ for cls in aborting_exceptions()}
    assert {
        "AgentPermissionError",
        "ProactivePermissionError",
        "RecipientNotAuthorizedError",
    } <= names


@pytest.mark.asyncio
async def test_a_guard_rejection_that_escapes_a_connector_still_aborts() -> None:
    from agents_system.services.sql_guard import QueryRejectedError

    runtime, _, _ = _runtime(
        _raising(QueryRejectedError("write", "writes are not allowed")),
        [_call(), AIMessage(content="unreachable")],
    )

    with pytest.raises(QueryRejectedError):
        await _turn(runtime)


@pytest.mark.asyncio
async def test_cancellation_still_propagates() -> None:
    """A cancelled turn (this is how the total timeout stops one) is never
    swallowed into an error result."""
    started = asyncio.Event()

    async def connector(inputs: dict[str, Any], *, session: Any) -> Any:
        started.set()
        await asyncio.sleep(60)

    runtime, model, _ = _runtime(connector, [_call(), AIMessage(content="unreachable")])
    task = asyncio.ensure_future(_turn(runtime))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert model.calls == 1


@pytest.mark.asyncio
async def test_a_tool_raising_cancellation_is_not_turned_into_an_error_result() -> None:
    """LangGraph re-labels a node's own CancelledError; either way the turn
    aborts and the model is never asked to react to it."""
    from langgraph.errors import NodeCancelledError

    runtime, model, _ = _runtime(
        _raising(asyncio.CancelledError()), [_call(), AIMessage(content="unreachable")]
    )

    with pytest.raises((asyncio.CancelledError, NodeCancelledError)):
        await _turn(runtime)

    assert model.calls == 1


@pytest.mark.asyncio
async def test_a_revoked_permission_is_still_returned_as_a_blocked_result() -> None:
    """The interceptor's denials were never raised out of the turn: they are
    already error results, and they do not count toward the error cap."""
    spec = ToolSpec(
        name="sender",
        required_permissions=("send:proactive",),
        connector=lambda inputs: {"sent": True},
        tier=Tier.T2,
        description="Sends",
        input_schema={"type": "object", "properties": {}},
    )
    equipped = EquippedRuntime(
        definition=_definition(),
        system_prompt="p",
        tools=(spec,),
        denied_tools=(),
        skills=(),
    )
    model = _ScriptedModel(
        responses=[_call("sender", "c1"), AIMessage(content="understood")]
    )
    runtime = AgentRuntime(equipped, model, metrics=build_metrics(CollectorRegistry()))

    messages = await runtime.run_turn(
        [HumanMessage(content="go")], session_id="s", permissions=()
    )

    (tool_message,) = _tool_messages(messages)
    assert tool_message.status == "error"
    assert "blocked" in tool_message.content
    assert messages[-1].content == "understood"


# ---------------------------------------------------------------------------
# max_tool_errors and the reply are validated like the other #278 settings
# ---------------------------------------------------------------------------


def test_max_tool_errors_is_a_deployment_limit_and_must_be_a_positive_whole_number() -> (
    None
):
    assert validate_execution_limits({"max_tool_errors": 5}, who="r") == {
        "max_tool_errors": 5
    }
    for bad in (0, -1, 1.5, True, float("inf")):
        with pytest.raises(ValueError, match="max_tool_errors"):
            validate_execution_limits({"max_tool_errors": bad}, who="r")


def test_the_tool_error_reply_is_plain_non_blank_text_of_bounded_size() -> None:
    assert validate_tool_error_reply("Sorry.", who="r") == "Sorry."
    with pytest.raises(ValueError, match="tool error reply"):
        validate_tool_error_reply("  ", who="r")
    with pytest.raises(ValueError, match="tool error reply"):
        validate_tool_error_reply("x" * 1001, who="r")
