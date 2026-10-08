"""Schedules through the real runtime and interceptor (#175, ADR-011).

A model asks for a schedule over a channel turn; later the runner runs it as a
turn for the same user, under the runtime's grants, and delivers text only to
that user.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agents_system.agent.graph import AgentRuntime
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition
from agents_system.permissions import permission_registry
from agents_system.proactive import (
    InMemoryProactiveQueue,
    ProactiveSender,
    StaticRecipientDirectory,
)
from agents_system.schedules import (
    InMemoryScheduleStore,
    ScheduledTurnRunner,
    build_schedule_tool_specs,
    schedule_backend_for,
)
from agents_system.scope import InMemoryScopeProvider
from agents_system.subject import TurnActor, TurnSubject

RT = "rt"
ALICE = TurnSubject.for_participant(1, channel="whatsapp", address="+100")
GRANTS = ("write:schedule",)


class _Model(FakeMessagesListChatModel):
    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _Model:
        return self


def _create_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "create_schedule",
                "args": {
                    "prompt": "Send me the weekly sales summary",
                    "frequency": "weekly",
                    "time": "08:00",
                    "weekday": "monday",
                    "timezone": "UTC",
                },
                "id": call_id,
            }
        ],
    )


def _equipped(grants: Sequence[str] = GRANTS) -> EquippedRuntime:
    definition = AgentDefinition(
        role_name="r",
        version="1.0",
        deployment=None,
        system_prompt="You help.",
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
        untrusted_input=False,
    )
    return EquippedRuntime(
        definition=definition,
        system_prompt="You help.",
        tools=build_schedule_tool_specs(),
        denied_tools=(),
        skills=(),
        deploy_grant_ceiling=frozenset(permission_registry.resolve(n) for n in grants),
    )


def _agent(
    model: _Model, provider: InMemoryScopeProvider, grants: Sequence[str] = GRANTS
) -> AgentRuntime:
    return AgentRuntime(
        _equipped(grants), model, runtime_id=RT, scope_provider=provider
    )


async def _ask(agent: AgentRuntime, subject: TurnSubject | None) -> Any:
    return await agent.run_turn_with_usage(
        [HumanMessage(content="every Monday at 8, send me the weekly sales")],
        session_id="s",
        permissions=GRANTS,
        subject=subject,
        actor=TurnActor(kind="channel", id="whatsapp"),
    )


async def test_a_channel_turn_creates_a_schedule_bound_to_its_subject() -> None:
    provider = InMemoryScopeProvider()
    agent = _agent(
        _Model(responses=[_create_call(), AIMessage(content="Done")]), provider
    )

    await _ask(agent, ALICE)

    stored = await schedule_backend_for(provider.scope(RT, ALICE.key)).list()
    assert [s.prompt for s in stored] == ["Send me the weekly sales summary"]
    assert stored[0].subject_key == ALICE.key


async def test_a_turn_without_a_subject_cannot_create_one() -> None:
    provider = InMemoryScopeProvider()
    agent = _agent(
        _Model(responses=[_create_call(), AIMessage(content="Done")]), provider
    )

    result = await _ask(agent, None)

    tool_messages = [m for m in result.messages if isinstance(m, ToolMessage)]
    assert tool_messages and tool_messages[0].status == "error"
    assert provider.all_rows("subject_schedules") == []


async def test_without_the_deploy_grant_the_tool_is_not_even_equipped() -> None:
    provider = InMemoryScopeProvider()
    agent = _agent(
        _Model(responses=[_create_call(), AIMessage(content="Done")]),
        provider,
        grants=(),
    )

    result = await _ask(agent, ALICE)

    assert provider.all_rows("subject_schedules") == []
    assert not any(
        isinstance(m, ToolMessage) and m.status != "error" for m in result.messages
    )


async def test_the_scheduled_run_goes_through_the_runtime_for_the_owner_only() -> None:
    provider = InMemoryScopeProvider()
    creator = _agent(
        _Model(responses=[_create_call(), AIMessage(content="Done")]), provider
    )
    await _ask(creator, ALICE)

    # The scheduled run's model answers directly: it has no way to name a recipient.
    runner_model = _Model(responses=[AIMessage(content="Sales this week: 42")])
    store = InMemoryScheduleStore(provider)
    queue = InMemoryProactiveQueue()
    directory = StaticRecipientDirectory({(RT, ALICE.key): ALICE})
    grants = {RT: {"write:schedule", "send:proactive"}}
    runner = ScheduledTurnRunner(
        store,
        {RT: _agent(runner_model, provider)},
        ProactiveSender(queue, directory, deploy_grants=grants),
        directory,
        deploy_grants=grants,
    )
    first = (await schedule_backend_for(provider.scope(RT, ALICE.key)).list())[0]
    outcome = await runner.run_once(now=first.next_run_at + timedelta(minutes=1))

    assert outcome.status == "delivered", outcome
    message = await queue.claim(
        "w", now=first.next_run_at + timedelta(days=1), lease_s=60
    )
    assert message is not None
    assert (message.subject_key, message.text) == (ALICE.key, "Sales this week: 42")
    assert message.actor == f"scheduler:{first.id}"


@pytest.mark.parametrize("granted", [(), ("send:proactive",)])
async def test_a_deployment_that_no_longer_grants_write_schedule_runs_nothing(
    granted: tuple[str, ...],
) -> None:
    provider = InMemoryScopeProvider()
    creator = _agent(
        _Model(responses=[_create_call(), AIMessage(content="Done")]), provider
    )
    await _ask(creator, ALICE)
    model = _Model(responses=[AIMessage(content="x")])
    directory = StaticRecipientDirectory({(RT, ALICE.key): ALICE})
    grants = {RT: set(granted)}
    runner = ScheduledTurnRunner(
        InMemoryScheduleStore(provider),
        {RT: _agent(model, provider)},
        ProactiveSender(InMemoryProactiveQueue(), directory, deploy_grants=grants),
        directory,
        deploy_grants=grants,
    )
    first = (await schedule_backend_for(provider.scope(RT, ALICE.key)).list())[0]

    outcome = await runner.run_once(now=first.next_run_at + timedelta(minutes=1))

    assert (outcome.status, outcome.reason) == ("skipped", "not_permitted")
