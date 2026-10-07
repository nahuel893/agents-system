"""ADR-011 parts 1 and 2: the turn subject and actor, bound per turn (#255)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
import structlog
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from agents_system.agent.graph import AgentRuntime
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition
from agents_system.subject import (
    TurnActor,
    TurnSubject,
    current_actor,
    current_subject,
    turn_scope,
)


def test_subject_and_actor_are_frozen_values() -> None:
    subject = TurnSubject.for_participant("7", channel="whatsapp", address="+54911")
    actor = TurnActor(kind="channel", id="whatsapp")
    assert subject.key == "participant:7"
    assert actor.label == "channel:whatsapp"
    with pytest.raises(dataclasses.FrozenInstanceError):
        subject.key = "participant:8"  # type: ignore[misc]


def test_actor_kind_is_a_closed_set() -> None:
    with pytest.raises(ValueError):
        TurnActor(kind="model", id="x")  # type: ignore[arg-type]


def test_turn_scope_binds_and_restores() -> None:
    subject = TurnSubject.for_participant("7", channel="api")
    actor = TurnActor(kind="api", id="rt")
    assert current_subject() is None and current_actor() is None
    with turn_scope(subject, actor):
        assert current_subject() == subject
        assert current_actor() == actor
        bound = structlog.contextvars.get_contextvars()
        assert bound["subject"] == "participant:7"
        assert bound["actor"] == "api:rt"
        assert "+54911" not in str(bound)
    assert current_subject() is None and current_actor() is None
    assert "subject" not in structlog.contextvars.get_contextvars()


@pytest.mark.asyncio
async def test_audit_events_carry_actor_and_subject_key_not_address() -> None:
    from agents_system.audit.recorder import record_tool_granted

    class _Def:
        role_name = "r"
        deployment = "d"

    subject = TurnSubject.for_participant("7", channel="whatsapp", address="+54911")
    with turn_scope(subject, TurnActor(kind="channel", id="whatsapp")):
        event = await record_tool_granted(_Def(), tool_name="t")
    assert event.actor == "channel:whatsapp"
    assert event.subject == "participant:7"
    assert "+54911" not in event.model_dump_json()


@pytest.mark.asyncio
async def test_audit_event_without_scope_is_unchanged() -> None:
    from agents_system.audit.recorder import record_tool_granted

    class _Def:
        role_name = "r"
        deployment = "d"

    event = await record_tool_granted(_Def(), tool_name="t")
    assert event.actor is None and event.subject is None


def test_mapper_persists_subject_column() -> None:
    from agents_system.audit.recorder import PLACEHOLDER_SEQUENCE  # noqa: F401
    from agents_system.models.audit_event import map_to_audit_event

    row = map_to_audit_event(
        {
            "event_id": __import__("uuid").uuid4(),
            "correlation_id": "c",
            "sequence": 1,
            "event_type": "tool_granted",
            "payload": {},
            "actor": "channel:whatsapp",
            "subject": "participant:7",
        }
    )
    assert row.subject == "participant:7"
    assert row.actor == "channel:whatsapp"


def _runtime() -> AgentRuntime:
    definition = AgentDefinition(
        role_name="r",
        version="1.0",
        deployment=None,
        system_prompt="p",
        tools=(),
        skills=(),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )
    equipped = EquippedRuntime(
        definition=definition,
        system_prompt="p",
        tools=(),
        denied_tools=(),
        skills=(),
    )

    class _Model(FakeMessagesListChatModel):
        def bind_tools(self, tools: Any, **kw: Any) -> Any:  # type: ignore[override]
            return self

    return AgentRuntime(
        equipped, _Model(responses=[AIMessage(content="hi")]), runtime_id="rt"
    )


@pytest.mark.asyncio
async def test_run_turn_binds_scope_and_logs_subject_and_actor() -> None:
    runtime = _runtime()
    subject = TurnSubject.for_participant("7", channel="whatsapp", address="+54911")
    actor = TurnActor(kind="channel", id="whatsapp")
    with structlog.testing.capture_logs() as logs:
        await runtime.run_turn(
            [HumanMessage(content="x")], "s1", subject=subject, actor=actor
        )
    usage_logs = [e for e in logs if e["event"] == "runtime.turn_usage"]
    assert usage_logs[0]["subject"] == "participant:7"
    assert usage_logs[0]["actor"] == "channel:whatsapp"
    assert "+54911" not in str(usage_logs)
    assert current_subject() is None


@pytest.mark.asyncio
async def test_run_turn_without_subject_logs_none_and_still_works() -> None:
    runtime = _runtime()
    with structlog.testing.capture_logs() as logs:
        result = await runtime.run_turn([HumanMessage(content="x")], "s1")
    assert result[-1].content == "hi"
    usage_logs = [e for e in logs if e["event"] == "runtime.turn_usage"]
    assert usage_logs[0].get("subject") is None
    assert usage_logs[0].get("actor") is None
