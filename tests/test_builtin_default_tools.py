"""Issue #168 -- a minimal agent needs only its own tools.

Every role-defined agent extends `platform/roles/agent`, whose manifest
declares `escalation_notifier`, so even a one-tool agent used to fail with
`InjectionError: Unknown tool: escalation_notifier` unless the importer also
registered an escalation tool it had no channel for.

In-process library use (`build_runtime` -> `AgentRuntime`) now gets a
built-in default for that one tool when the registry lacks it. The default
reaches NO human: it says so to the model, logs a WARNING and records an
`escalation_not_delivered` audit event on every call. The served app keeps
failing closed (`tests/test_main.py`); no predefined role changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
import structlog
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agents_system
from agents_system.audit.events import AuditEvent, EscalationNotDelivered
from agents_system.audit.sink import AuditSink
from agents_system.harness.defaults import builtin_default_tools, is_builtin_default
from agents_system.harness.injector import InjectionError


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> ScriptedModel:
        return self


class CapturingSink(AuditSink):
    """Real sink and drainer; only the database write is replaced."""

    def __init__(self) -> None:
        super().__init__(session_factory=None, maxsize=100)  # type: ignore[arg-type]
        self.captured: list[Any] = []

    async def _flush_batch(self, batch: list[Any]) -> None:
        self.captured.extend(batch)


@pytest.fixture
async def sink() -> AsyncIterator[CapturingSink]:
    capturing = CapturingSink()
    await capturing.start()
    AuditSink.set_current(capturing)
    try:
        yield capturing
    finally:
        await capturing.stop()
        AuditSink.clear_current(capturing)


def word_count(inputs: dict[str, Any], /) -> dict[str, Any]:
    return {"words": len(str(inputs.get("text", "")).split())}


def _registry(*specs: agents_system.ToolSpec) -> agents_system.ToolRegistry:
    registry = agents_system.ToolRegistry()
    registry.register(
        agents_system.ToolSpec(
            name="word_count",
            required_permissions=(),
            connector=word_count,
            tier=agents_system.Tier.T0,
        )
    )
    for spec in specs:
        registry.register(spec)
    return registry


AGENT = agents_system.Agent(name="word-counter", tools=("word_count",))


def test_build_runtime_accepts_an_agent_directly() -> None:
    equipped = agents_system.build_runtime(AGENT, _registry(), granted_permissions=[])

    assert equipped.definition.role_name == "word-counter"
    assert agents_system.resolve(AGENT).role_name == "word-counter"


def test_one_custom_tool_is_enough_and_the_registry_is_left_alone() -> None:
    registry = _registry()

    equipped = agents_system.build_runtime(AGENT, registry, granted_permissions=[])

    assert [tool.name for tool in equipped.tools] == ["word_count"]
    # Declared by the parent role, filled by the default, and still denied:
    # nobody granted send:escalation, so it never reaches the model.
    assert dict(equipped.denied_tools) == {
        "escalation_notifier": "missing permissions: send:escalation"
    }
    assert registry.names() == ("word_count",)


def test_a_registered_escalation_notifier_wins_over_the_default() -> None:
    async def page_a_human(
        inputs: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        return {"escalation_id": "esc-1"}

    own = agents_system.ToolSpec(
        name="escalation_notifier",
        required_permissions=("send:escalation",),
        connector=page_a_human,
        tier=agents_system.Tier.T2,
    )

    equipped = agents_system.build_runtime(
        AGENT, _registry(own), granted_permissions=["send:escalation"]
    )

    [escalation] = [t for t in equipped.tools if t.name == "escalation_notifier"]
    assert escalation is own
    assert not is_builtin_default(escalation)


def test_any_other_unregistered_tool_still_fails_loud() -> None:
    typo = agents_system.Agent(name="typo", tools=("word_cuont",))

    with pytest.raises(InjectionError, match="Unknown tool: word_cuont"):
        agents_system.build_runtime(typo, _registry(), granted_permissions=[])


def test_builtin_defaults_false_keeps_the_fail_closed_error() -> None:
    with pytest.raises(InjectionError, match="Unknown tool: escalation_notifier"):
        agents_system.build_runtime(
            AGENT, _registry(), granted_permissions=[], builtin_defaults=False
        )


def test_the_default_keeps_the_platform_contract_for_the_tool() -> None:
    definition = agents_system.resolve(AGENT)
    spec = builtin_default_tools(definition)["escalation_notifier"]

    assert is_builtin_default(spec)
    assert spec.tier is agents_system.Tier.T2
    assert spec.required_permissions == ("send:escalation",)
    assert "escalation_id" in spec.description
    assert spec.input_schema["required"] == ["reason", "details"]


async def test_a_granted_default_escalation_reaches_no_human_and_says_so(
    sink: CapturingSink,
) -> None:
    with structlog.testing.capture_logs() as build_logs:
        equipped = agents_system.build_runtime(
            AGENT, _registry(), granted_permissions=["send:escalation"]
        )
    [escalation] = [t for t in equipped.tools if t.name == "escalation_notifier"]
    assert is_builtin_default(escalation)
    assert any(
        entry["event"] == "injector.builtin_default_granted"
        and entry["log_level"] == "warning"
        for entry in build_logs
    ), build_logs

    model = ScriptedModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "escalation_notifier",
                        "args": {"reason": "user_request", "details": "wants a person"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="I could not reach a human."),
        ]
    )
    runtime = agents_system.AgentRuntime(equipped, model=model)

    with structlog.testing.capture_logs() as call_logs:
        messages = await runtime.run_turn(
            [HumanMessage(content="Let me talk to a person")], session_id="s1"
        )

    [tool_message] = [m for m in messages if isinstance(m, ToolMessage)]
    assert "NO human was notified" in str(tool_message.content)
    assert "escalation_id" not in str(tool_message.content)

    warnings = [
        entry
        for entry in call_logs
        if entry["event"] == "escalation.not_delivered"
        and entry["log_level"] == "warning"
    ]
    assert len(warnings) == 1, call_logs
    assert warnings[0]["reason"] == "user_request"
    assert warnings[0]["role"] == "word-counter"
    # `details` is free text about the customer, so it stays out of the log
    # line, as it does for the real tool (`escalation.failed` logs only the
    # reason); the audit event carries it through the audit redactor.
    assert "wants a person" not in str(warnings[0]), warnings[0]
    assert warnings[0]["details_chars"] == len("wants a person")

    for _ in range(50):
        delivered = [e for e in sink.captured if isinstance(e, EscalationNotDelivered)]
        if delivered:
            break
        await asyncio.sleep(0.02)
    assert len(delivered) == 1, sink.captured
    event = delivered[0]
    assert event.event_type == "escalation_not_delivered"
    assert event.tool_name == "escalation_notifier"
    assert event.role == "word-counter"
    assert event.payload["reason"] == "user_request"


def test_the_audit_event_round_trips_through_the_union() -> None:
    event = AuditEvent.model_validate(
        {
            "event_type": "escalation_not_delivered",
            "event_id": "00000000-0000-0000-0000-000000000001",
            "occurred_at": "2026-01-01T00:00:00Z",
            "correlation_id": "c",
            "sequence": 1,
            "role": "word-counter",
            "tool_name": "escalation_notifier",
            "reason": "user_request",
        }
    )

    assert isinstance(event, EscalationNotDelivered)
