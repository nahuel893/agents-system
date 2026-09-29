"""Context-size control for the model input (issue #28, ADR-002 G.24).

`_call_model` used to send the whole message history on every call. A long
conversation would eventually exceed the model's context window and the
provider would answer with a hard error. The runtime now trims the OLDEST
history before each model call, inside the model-input projection only:
the persisted history is never touched.

Rules pinned here:
  - a history within the budget is sent unmodified (no premature trimming);
  - the system prompt, the latest user message and everything the model
    needs to keep a tool round trip well formed are never dropped;
  - an assistant tool call is never split from its tool replies (#181);
  - the budget is `execution_limits.max_context_tokens`, per agent, with a
    platform default;
  - trimming is logged, audited and counted.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import structlog.testing
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.checkpoint.memory import InMemorySaver

from agents_system.audit.events import ContextTrimmed
from agents_system.audit.sink import AuditSink
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _text(label: str, chars: int = 400) -> str:
    return (label + " ").ljust(chars, "x")


def _exchange(index: int, chars: int = 400) -> list[AnyMessage]:
    return [
        HumanMessage(content=_text(f"question-{index}", chars)),
        AIMessage(content=_text(f"answer-{index}", chars)),
    ]


def _conversation(exchanges: int, chars: int = 400) -> list[AnyMessage]:
    history: list[AnyMessage] = []
    for index in range(exchanges):
        history.extend(_exchange(index, chars))
    return history


def _tool_round(index: int, chars: int = 300) -> list[AnyMessage]:
    """One assistant message with two tool calls plus both replies."""
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": f"call-{index}-a",
                    "name": "catalog_search",
                    "args": {"q": _text("a", 40)},
                    "type": "tool_call",
                },
                {
                    "id": f"call-{index}-b",
                    "name": "catalog_search",
                    "args": {"q": _text("b", 40)},
                    "type": "tool_call",
                },
            ],
        ),
        ToolMessage(
            content=_text(f"result-{index}-a", chars), tool_call_id=f"call-{index}-a"
        ),
        ToolMessage(
            content=_text(f"result-{index}-b", chars), tool_call_id=f"call-{index}-b"
        ),
    ]


def _assert_well_formed(messages: Sequence[AnyMessage]) -> None:
    """No orphan tool reply, no unanswered tool call, starts on a user turn."""
    if not messages:
        return
    assert isinstance(messages[0], HumanMessage), messages[0]
    for index, message in enumerate(messages):
        if isinstance(message, ToolMessage):
            # Walk back over sibling replies to the assistant message.
            back = index - 1
            while back >= 0 and isinstance(messages[back], ToolMessage):
                back -= 1
            assert back >= 0, "tool reply without an assistant message"
            owner = messages[back]
            assert isinstance(owner, AIMessage)
            assert message.tool_call_id in {c["id"] for c in owner.tool_calls}
        if isinstance(message, AIMessage) and message.tool_calls:
            replies = set()
            for later in messages[index + 1 :]:
                if not isinstance(later, ToolMessage):
                    break
                replies.add(later.tool_call_id)
            assert {c["id"] for c in message.tool_calls} <= replies


# ---------------------------------------------------------------------------
# trim_history -- the pure mechanism
# ---------------------------------------------------------------------------


def test_history_within_budget_is_returned_unmodified() -> None:
    from agents_system.agent.context import trim_history

    history = _conversation(4)
    result = trim_history(history, max_tokens=100_000)

    assert result.messages == history
    assert result.dropped_messages == 0
    assert result.trimmed is False
    assert result.over_budget is False


def test_history_over_budget_drops_the_oldest_and_fits_the_budget() -> None:
    from agents_system.agent.context import trim_history

    history = _conversation(20)
    budget = 1_000
    result = trim_history(history, max_tokens=budget)

    assert result.trimmed
    assert result.dropped_messages == len(history) - len(result.messages)
    assert result.tokens_before > budget
    assert result.tokens_after <= budget
    assert result.over_budget is False
    # The kept messages are the newest ones, in order, untouched.
    assert result.messages == history[len(history) - len(result.messages) :]
    assert result.messages[-1] is history[-1]
    _assert_well_formed(result.messages)


def test_fixed_tokens_shrink_the_room_left_for_history() -> None:
    from agents_system.agent.context import trim_history

    history = _conversation(20)
    roomy = trim_history(history, max_tokens=2_000)
    cramped = trim_history(history, max_tokens=2_000, fixed_tokens=1_500)

    assert len(cramped.messages) < len(roomy.messages)
    assert cramped.tokens_after <= 2_000


def test_latest_user_message_is_kept_even_when_it_alone_exceeds_the_budget() -> None:
    from agents_system.agent.context import trim_history

    history = [*_conversation(3), HumanMessage(content=_text("latest", 8_000))]
    result = trim_history(history, max_tokens=100)

    assert result.messages == [history[-1]]
    assert result.over_budget is True
    assert result.dropped_messages == len(history) - 1


@pytest.mark.parametrize("budget", range(0, 1_600, 50))
def test_a_tool_call_is_never_split_from_its_replies(budget: int) -> None:
    from agents_system.agent.context import trim_history

    history: list[AnyMessage] = [
        *_conversation(3),
        HumanMessage(content=_text("look it up")),
        *_tool_round(1),
        *_tool_round(2),
        AIMessage(content=_text("found it")),
        HumanMessage(content=_text("and the price?")),
        *_tool_round(3),
        *_tool_round(4),
        *_tool_round(5),
    ]
    result = trim_history(history, max_tokens=budget)

    _assert_well_formed(result.messages)
    # The latest user message survives every budget.
    assert history[-len(_tool_round(0)) * 3 - 1] in result.messages


def test_current_turn_keeps_its_newest_tool_rounds_when_history_cannot_fit() -> None:
    from agents_system.agent.context import trim_history

    user = HumanMessage(content=_text("do a long job"))
    rounds = [msg for i in range(6) for msg in _tool_round(i)]
    history: list[AnyMessage] = [*_conversation(2), user, *rounds]
    # Room for the user message and about two tool rounds.
    budget = count_tokens_approximately([user, *_tool_round(0), *_tool_round(1)]) + 20
    result = trim_history(history, max_tokens=budget)

    assert result.messages[0] is user
    assert result.messages[-1] is history[-1]
    assert result.tokens_after <= budget
    _assert_well_formed(result.messages)


def test_a_history_without_a_user_message_does_not_crash() -> None:
    from agents_system.agent.context import trim_history

    history: list[AnyMessage] = [AIMessage(content=_text("a")) for _ in range(5)]
    result = trim_history(history, max_tokens=10)

    assert result.messages == [history[-1]]


def test_an_empty_history_is_returned_as_is() -> None:
    from agents_system.agent.context import trim_history

    result = trim_history([], max_tokens=10)

    assert result.messages == []
    assert result.trimmed is False


def test_the_input_history_is_not_mutated() -> None:
    from agents_system.agent.context import trim_history

    history = _conversation(20)
    snapshot = list(history)
    trim_history(history, max_tokens=500)

    assert history == snapshot


# ---------------------------------------------------------------------------
# Budget configuration
# ---------------------------------------------------------------------------


def test_the_platform_default_budget_is_derived_from_the_assumed_window() -> None:
    from agents_system.agent.context import (
        ASSUMED_CONTEXT_WINDOW_TOKENS,
        CONTEXT_BUDGET_FRACTION,
    )
    from agents_system.harness.loader import PLATFORM_DEFAULT_LIMITS

    assert (
        PLATFORM_DEFAULT_LIMITS["max_context_tokens"]
        == ASSUMED_CONTEXT_WINDOW_TOKENS * CONTEXT_BUDGET_FRACTION
    )


def test_effective_limits_carry_the_context_budget() -> None:
    from agents_system.agent.graph import _effective_limits
    from agents_system.harness.loader import PLATFORM_DEFAULT_LIMITS

    assert (
        _effective_limits(None)["max_context_tokens"]
        == PLATFORM_DEFAULT_LIMITS["max_context_tokens"]
    )
    assert (
        _effective_limits({"max_context_tokens": 5_000})["max_context_tokens"] == 5_000
    )
    assert (
        _effective_limits({"max_context_tokens": None})["max_context_tokens"]
        == PLATFORM_DEFAULT_LIMITS["max_context_tokens"]
    )


def test_a_definition_cannot_raise_the_budget_above_the_platform_default() -> None:
    from agents_system.harness.loader import (
        PLATFORM_DEFAULT_LIMITS,
        DefinitionError,
        _validate_execution_limits,
    )

    _validate_execution_limits({}, {"max_context_tokens": 8_000})
    with pytest.raises(DefinitionError, match="max_context_tokens"):
        _validate_execution_limits(
            {},
            {"max_context_tokens": PLATFORM_DEFAULT_LIMITS["max_context_tokens"] + 1},
        )


# ---------------------------------------------------------------------------
# The runtime -- what the model actually receives
# ---------------------------------------------------------------------------


class _RecordingModel(FakeMessagesListChatModel):
    """Records every message list the model is invoked with."""

    captured_inputs: list[list[Any]] = []  # noqa: RUF012

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _RecordingModel:
        return self

    async def ainvoke(self, input: Any, *args: Any, **kwargs: Any) -> AIMessage:  # type: ignore[override]
        self.captured_inputs.append(list(input))
        return await super().ainvoke(input, *args, **kwargs)


def _model(replies: int = 1) -> _RecordingModel:
    model = _RecordingModel(
        responses=[AIMessage(content=f"reply-{i}") for i in range(replies)]
    )
    model.captured_inputs = []
    return model


def _runtime(execution_limits: dict[str, Any] | None = None) -> EquippedRuntime:
    return EquippedRuntime(
        definition=AgentDefinition(
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
            untrusted_input=False,
        ),
        system_prompt="You are a helpful assistant.",
        tools=(),
        denied_tools=(),
        skills=(),
    )


@pytest.mark.asyncio
async def test_the_model_receives_a_trimmed_input_within_the_budget() -> None:
    from agents_system.agent.graph import AgentRuntime

    budget = 1_200
    model = _model()
    agent = AgentRuntime(_runtime({"max_context_tokens": budget}), model)
    history = [*_conversation(30), HumanMessage(content="latest question")]

    result = await agent.run_turn(history, session_id="s1", permissions=())

    [model_input] = model.captured_inputs
    assert isinstance(model_input[0], SystemMessage)
    assert model_input[0].content == "You are a helpful assistant."
    assert model_input[-1].content == "latest question"
    assert count_tokens_approximately(model_input) <= budget
    assert len(model_input) < len(history) + 1
    _assert_well_formed(model_input[1:])
    # The trimmed history is a projection: the turn's returned messages still
    # hold every message the caller sent, plus the reply.
    assert result[: len(history)] == history
    assert result[-1].content == "reply-0"


@pytest.mark.asyncio
async def test_a_conversation_under_the_budget_reaches_the_model_unmodified() -> None:
    from agents_system.agent.graph import AgentRuntime

    model = _model()
    agent = AgentRuntime(_runtime({"max_context_tokens": 100_000}), model)
    history = [*_conversation(5), HumanMessage(content="latest question")]

    with structlog.testing.capture_logs() as logs:
        await agent.run_turn(history, session_id="s1", permissions=())

    [model_input] = model.captured_inputs
    assert model_input[1:] == history
    assert not [e for e in logs if e["event"] == "runtime.context_trimmed"]


@pytest.mark.asyncio
async def test_the_default_budget_applies_when_the_agent_sets_none() -> None:
    from agents_system.agent.graph import AgentRuntime
    from agents_system.harness.loader import PLATFORM_DEFAULT_LIMITS

    default = PLATFORM_DEFAULT_LIMITS["max_context_tokens"]
    model = _model()
    agent = AgentRuntime(_runtime(), model)
    # About twice the default budget: 400 chars is about 100 tokens a message.
    history = [*_conversation(int(default / 100)), HumanMessage(content="latest")]

    await agent.run_turn(history, session_id="s1", permissions=())

    [model_input] = model.captured_inputs
    assert count_tokens_approximately(model_input) <= default
    assert model_input[-1].content == "latest"
    assert len(model_input) < len(history)


@pytest.mark.asyncio
async def test_trimming_is_logged() -> None:
    from agents_system.agent.graph import AgentRuntime

    agent = AgentRuntime(_runtime({"max_context_tokens": 800}), _model())
    history = [*_conversation(20), HumanMessage(content="latest")]

    with structlog.testing.capture_logs() as logs:
        await agent.run_turn(history, session_id="s1", permissions=())

    [entry] = [e for e in logs if e["event"] == "runtime.context_trimmed"]
    assert entry["log_level"] == "warning"
    assert entry["max_context_tokens"] == 800
    assert entry["dropped_messages"] > 0
    assert entry["tokens_before"] > 800
    assert entry["tokens_after"] <= 800
    assert entry["over_budget"] is False
    # Message text never reaches the log.
    assert "question-" not in str(entry)


class _CapturingSink(AuditSink):
    def __init__(self) -> None:
        super().__init__(session_factory=None, maxsize=100)  # type: ignore[arg-type]
        self.captured: list[Any] = []

    async def _flush_batch(self, batch: list[Any]) -> None:
        self.captured.extend(batch)


@pytest.fixture
async def sink() -> AsyncIterator[_CapturingSink]:
    capturing = _CapturingSink()
    await capturing.start()
    AuditSink.set_current(capturing)
    try:
        yield capturing
    finally:
        await capturing.stop()
        AuditSink.clear_current(capturing)


@pytest.mark.asyncio
async def test_trimming_is_audited_and_counted(sink: _CapturingSink) -> None:
    from prometheus_client import CollectorRegistry

    from agents_system.agent.graph import AgentRuntime
    from agents_system.observability.metrics import build_metrics

    metrics = build_metrics(CollectorRegistry())
    agent = AgentRuntime(
        _runtime({"max_context_tokens": 800}), _model(), metrics=metrics
    )
    history = [*_conversation(20), HumanMessage(content="latest")]

    await agent.run_turn(history, session_id="s1", permissions=())

    for _ in range(50):
        events = [e for e in sink.captured if isinstance(e, ContextTrimmed)]
        if events:
            break
        await asyncio.sleep(0.02)
    [event] = events
    assert event.event_type == "context_trimmed"
    assert event.role == "sales-agent"
    assert event.max_context_tokens == 800
    assert event.dropped_messages > 0
    assert event.tokens_after <= 800 < event.tokens_before
    assert event.over_budget is False
    # No conversation text in the audit payload.
    assert "question-" not in str(event.payload)
    assert (
        metrics.limit_trips_total.labels(limit="max_context_tokens")._value.get() == 1
    )


@pytest.mark.asyncio
async def test_an_untrimmed_turn_emits_no_audit_event(sink: _CapturingSink) -> None:
    from agents_system.agent.graph import AgentRuntime

    agent = AgentRuntime(_runtime({"max_context_tokens": 100_000}), _model())

    await agent.run_turn(_conversation(3), session_id="s1", permissions=())
    await asyncio.sleep(0.1)

    assert not [e for e in sink.captured if isinstance(e, ContextTrimmed)]


@pytest.mark.asyncio
async def test_a_checkpointed_thread_keeps_its_full_history_while_the_input_is_trimmed() -> (
    None
):
    from agents_system.agent.graph import AgentRuntime

    model = _model(replies=6)
    agent = AgentRuntime(
        _runtime({"max_context_tokens": 700}), model, checkpointer=InMemorySaver()
    )

    result: list[AnyMessage] = []
    for turn in range(6):
        result = await agent.run_turn(
            [HumanMessage(content=_text(f"turn-{turn}", 600))],
            session_id="s1",
            thread_id="thread-1",
            permissions=(),
        )

    # The last call saw a bounded input ...
    assert count_tokens_approximately(model.captured_inputs[-1]) <= 700
    assert len(model.captured_inputs[-1]) < 1 + 11
    # ... while the persisted thread holds all six user messages.
    humans = [m for m in result if isinstance(m, HumanMessage)]
    assert [m.content[:6] for m in humans] == [f"turn-{i}" for i in range(6)]


@pytest.mark.asyncio
async def test_a_persisted_unanswered_tool_call_is_still_repaired_then_trimmed() -> (
    None
):
    """#181 and #28 compose: the repair runs first, so trimming sees a well
    formed history and never has to cut inside an unanswered round."""
    from agents_system.agent.graph import AgentRuntime

    model = _model()
    agent = AgentRuntime(_runtime({"max_context_tokens": 900}), model)
    dangling = AIMessage(
        content="",
        tool_calls=[
            {"id": "lost", "name": "catalog_search", "args": {}, "type": "tool_call"}
        ],
    )
    history = [
        *_conversation(20),
        HumanMessage(content="first ask"),
        dangling,
        HumanMessage(content="are you there?"),
    ]

    await agent.run_turn(history, session_id="s1", permissions=())

    [model_input] = model.captured_inputs
    _assert_well_formed(model_input[1:])
    assert model_input[-1].content == "are you there?"


# ---------------------------------------------------------------------------
# Review fixes (PR #194): the newest tool round is mandatory, and an
# over-budget request is always reported
# ---------------------------------------------------------------------------


def test_the_newest_tool_round_is_kept_even_when_its_result_exceeds_the_budget() -> (
    None
):
    from agents_system.agent.context import trim_history

    user = HumanMessage(content="Please calculate")
    huge_round = _tool_round(1, chars=4_000)
    history: list[AnyMessage] = [*_conversation(2), user, *huge_round]

    result = trim_history(history, max_tokens=200)

    # The model must see the call it just made AND its result, or it cannot
    # tell the operation already ran and would issue it again.
    assert result.messages == [user, *huge_round]
    assert result.over_budget is True
    _assert_well_formed(result.messages)


def test_older_tool_rounds_still_give_way_to_the_mandatory_newest_round() -> None:
    from agents_system.agent.context import trim_history

    user = HumanMessage(content="Please calculate")
    history: list[AnyMessage] = [
        user,
        *_tool_round(0),
        *_tool_round(1, chars=4_000),
    ]

    result = trim_history(history, max_tokens=200)

    assert result.messages == [user, *history[-3:]]
    _assert_well_formed(result.messages)


def test_a_fixed_only_request_above_the_budget_is_flagged_over_budget() -> None:
    from agents_system.agent.context import trim_history

    result = trim_history([], max_tokens=100, fixed_tokens=500)

    assert result.messages == []
    assert result.over_budget is True
    assert result.trimmed is False


def _big_single_message_state(chars: int = 40_000) -> dict[str, Any]:
    return {"messages": [HumanMessage(content="x" * chars)]}


@pytest.mark.asyncio
async def test_an_over_budget_request_with_nothing_to_drop_is_logged() -> None:
    from agents_system.agent.graph import _call_model

    model = _model()
    with structlog.testing.capture_logs() as logs:
        await _call_model(
            _big_single_message_state(), model, "sys", max_context_tokens=1_000
        )

    [entry] = [e for e in logs if e["event"] == "runtime.context_trimmed"]
    assert entry["over_budget"] is True
    assert entry["dropped_messages"] == 0
    assert entry["tokens_after"] > entry["max_context_tokens"] == 1_000
    # The request is still sent: dropping the user's message answers nothing.
    assert len(model.captured_inputs[0][-1].content) == 40_000
    assert "xxxx" not in str(entry)


@pytest.mark.asyncio
async def test_an_over_budget_request_with_nothing_to_drop_is_audited_and_counted(
    sink: _CapturingSink,
) -> None:
    from prometheus_client import CollectorRegistry

    from agents_system.agent.graph import _call_model
    from agents_system.observability.metrics import build_metrics

    metrics = build_metrics(CollectorRegistry())
    await _call_model(
        _big_single_message_state(),
        _model(),
        "sys",
        max_context_tokens=1_000,
        definition=_runtime().definition,
        metrics=metrics,
    )

    for _ in range(50):
        events = [e for e in sink.captured if isinstance(e, ContextTrimmed)]
        if events:
            break
        await asyncio.sleep(0.02)
    [event] = events
    assert event.over_budget is True
    assert event.dropped_messages == 0
    assert (
        metrics.limit_trips_total.labels(limit="max_context_tokens")._value.get() == 1
    )


@pytest.mark.asyncio
async def test_a_fixed_parts_only_request_above_the_budget_is_reported() -> None:
    from agents_system.agent.graph import _call_model

    with structlog.testing.capture_logs() as logs:
        await _call_model(
            {"messages": []}, _model(), "s" * 4_000, max_context_tokens=100
        )

    [entry] = [e for e in logs if e["event"] == "runtime.context_trimmed"]
    assert entry["over_budget"] is True
    assert entry["dropped_messages"] == 0


@pytest.mark.asyncio
async def test_an_oversized_tool_result_reaches_the_model_with_its_call() -> None:
    from agents_system.agent.graph import _call_model

    model = _model()
    user = HumanMessage(content="Please calculate")
    huge_round = _tool_round(1, chars=4_000)
    await _call_model(
        {"messages": [user, *huge_round]}, model, "sys", max_context_tokens=200
    )

    assert model.captured_inputs[0][1:] == [user, *huge_round]
