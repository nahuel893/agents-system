"""`memory_policy` is enforced at the platform layer (#26, ADR-002 A.5).

A restrictive policy must block what a permissive one allows: a memory tool
call outside `read_scope`/`write_scope` is refused and audited before the
connector runs, and `persist_conversation: false` means the thread is never
read from or written to the checkpointer. Nothing here relies on the model.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from agents_system.agent.graph import AgentRuntime
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.interceptor import PolicyViolation, intercept
from agents_system.harness.registry import Tier, ToolSpec
from agents_system.memory_policy import MemoryPolicy, MemoryPolicyError
from agents_system.scope import InMemoryScopeProvider
from agents_system.subject import TurnActor, TurnSubject, turn_scope


def _definition(memory_policy: dict[str, Any]) -> Any:
    from agents_system.harness.loader import AgentDefinition

    return AgentDefinition(
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
        memory_policy=memory_policy,
        audit_policy={},
        execution_limits=None,
    )


def _equipped(policy: dict[str, Any], *tools: ToolSpec) -> EquippedRuntime:
    return EquippedRuntime(
        definition=_definition(policy),
        system_prompt="p",
        tools=tuple(tools),
        denied_tools=(),
        skills=(),
    )


# --- the policy value -------------------------------------------------------


def test_scopes_are_ordered_local_team_org() -> None:
    policy = MemoryPolicy.from_mapping({"read_scope": "team", "write_scope": "local"})
    assert policy.allows_read("local") and policy.allows_read("team")
    assert not policy.allows_read("org")
    assert policy.allows_write("local") and not policy.allows_write("team")


def test_the_shipped_spellings_session_and_deployment_are_accepted() -> None:
    # `platform/roles/*/policy.md` and `examples/agents/*/policy.md` use them.
    base = MemoryPolicy.from_mapping(
        {
            "read_scope": "session",
            "write_scope": "session",
            "persist_conversation": False,
        }
    )
    assert base.allows_read("local") and not base.allows_read("team")
    sales = MemoryPolicy.from_mapping(
        {"read_scope": "session", "write_scope": "deployment"}
    )
    assert sales.allows_write("org") and not sales.allows_read("org")
    assert sales.allows_write("session")


def test_every_shipped_policy_file_parses() -> None:
    import pathlib

    from agents_system.harness.loader import _read_md

    root = pathlib.Path(__file__).parent.parent
    files = [
        *root.glob("platform/roles/*/policy.md"),
        *root.glob("examples/agents/*/policy.md"),
    ]
    assert files
    for path in files:
        front, _body = _read_md(path)
        MemoryPolicy.from_mapping(front.get("memory_policy") or {})


def test_an_undeclared_policy_is_local_and_persists() -> None:
    policy = MemoryPolicy.from_mapping({})
    assert policy.allows_read("local") and policy.allows_write("local")
    assert not policy.allows_read("team")
    assert policy.persist_conversation is True


@pytest.mark.parametrize(
    "raw",
    [
        {"read_scope": "galaxy"},
        {"write_scope": 3},
        {"persist_conversation": "false"},
        {"surprise": 1},
    ],
)
def test_an_invalid_policy_fails_closed(raw: dict[str, Any]) -> None:
    with pytest.raises(MemoryPolicyError):
        MemoryPolicy.from_mapping(raw)


def test_toolspec_rejects_an_unknown_memory_access() -> None:
    with pytest.raises(ValueError):
        ToolSpec(
            name="t",
            required_permissions=(),
            connector=lambda i: i,
            tier=Tier.T1,
            memory_access="append",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError):
        ToolSpec(
            name="t",
            required_permissions=(),
            connector=lambda i: i,
            tier=Tier.T1,
            memory_access="read",
            memory_scope="galaxy",
        )


# --- read_scope / write_scope at the interceptor -----------------------------


def _memory_tool(access: str, scope: str, ran: list[str]) -> ToolSpec:
    async def connector(inputs: dict, *, session: Any = None, scope: Any) -> dict:
        ran.append(inputs.get("k", ""))
        return {"ok": True}

    return ToolSpec(
        name="remember",
        required_permissions=(),
        connector=connector,
        tier=Tier.T1,
        subject_scoped=True,
        memory_access=access,  # type: ignore[arg-type]
        memory_scope=scope,
    )


async def _call(policy: dict[str, Any], access: str, scope: str) -> list[str]:
    ran: list[str] = []
    runtime = _equipped(policy, _memory_tool(access, scope, ran))
    with turn_scope(TurnSubject(key="participant:a"), TurnActor("library", "t")):
        await intercept(
            "remember",
            {"k": "x"},
            runtime,
            current_permissions=(),
            scope_provider=InMemoryScopeProvider(),
            runtime_id="rt",
        )
    return ran


async def test_a_permissive_policy_allows_the_memory_write() -> None:
    assert await _call({"write_scope": "local"}, "write", "local") == ["x"]


async def test_a_restrictive_policy_blocks_the_same_write() -> None:
    # Same tool, same call; only the declared policy differs.
    with pytest.raises(PolicyViolation) as exc:
        await _call({"write_scope": "local"}, "write", "team")
    assert exc.value.reason == "memory_write_denied"


async def test_a_read_outside_read_scope_is_blocked_before_the_connector() -> None:
    ran: list[str] = []
    runtime = _equipped({"read_scope": "local"}, _memory_tool("read", "org", ran))
    with (
        turn_scope(TurnSubject(key="participant:a"), TurnActor("library", "t")),
        pytest.raises(PolicyViolation) as exc,
    ):
        await intercept(
            "remember",
            {"k": "x"},
            runtime,
            current_permissions=(),
            scope_provider=InMemoryScopeProvider(),
            runtime_id="rt",
        )
    assert exc.value.reason == "memory_read_denied"
    assert ran == []


async def test_readwrite_needs_both_scopes() -> None:
    with pytest.raises(PolicyViolation) as exc:
        await _call({"read_scope": "org", "write_scope": "local"}, "readwrite", "team")
    assert exc.value.reason == "memory_write_denied"


async def test_an_invalid_policy_blocks_memory_tools() -> None:
    with pytest.raises(PolicyViolation) as exc:
        await _call({"read_scope": "galaxy"}, "read", "local")
    assert exc.value.reason == "memory_policy_invalid"


async def test_tools_without_memory_access_ignore_the_policy() -> None:
    async def connector(inputs: dict, *, session: Any = None) -> dict:
        return {"ok": True}

    tool = ToolSpec(
        name="plain", required_permissions=(), connector=connector, tier=Tier.T1
    )
    runtime = _equipped({"read_scope": "galaxy"}, tool)
    result = await intercept("plain", {}, runtime, current_permissions=())
    assert result.output == {"ok": True}


# --- persist_conversation ----------------------------------------------------


class _Model(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> _Model:  # type: ignore[override]
        return self


class _Saver(InMemorySaver):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0
        self.puts = 0

    async def aget_tuple(self, config: Any) -> Any:
        self.reads += 1
        return await super().aget_tuple(config)

    async def aput(self, *args: Any, **kwargs: Any) -> Any:
        self.puts += 1
        return await super().aput(*args, **kwargs)


async def _turn(policy: dict[str, Any]) -> _Saver:
    saver = _Saver()
    agent = AgentRuntime(
        _equipped(policy),
        _Model(responses=[AIMessage(content="hi")]),
        checkpointer=saver,
    )
    await agent.run_turn_with_usage(
        [HumanMessage(content="hello")],
        session_id="s",
        permissions=(),
        thread_id="thread-1",
    )
    return saver


async def test_persist_conversation_true_writes_the_thread() -> None:
    saver = await _turn({"persist_conversation": True})
    assert saver.puts > 0


async def test_persist_conversation_false_never_touches_the_checkpointer() -> None:
    saver = await _turn({"persist_conversation": False})
    assert saver.puts == 0
    assert saver.reads == 0


async def test_an_invalid_policy_does_not_persist() -> None:
    saver = await _turn({"persist_conversation": "no"})
    assert saver.puts == 0


def test_the_runtime_exposes_the_decision_to_the_channels() -> None:
    agent = AgentRuntime(
        _equipped({"persist_conversation": False}), _Model(responses=[])
    )
    assert agent.persist_conversation is False
    assert AgentRuntime(_equipped({}), _Model(responses=[])).persist_conversation


def test_the_gate_is_visible_outside_the_loader() -> None:
    # Regression for the issue: the fields are read by enforcement, not only
    # folded by the loader.
    import pathlib
    import re

    src = pathlib.Path(__file__).parent.parent / "src" / "agents_system"
    pattern = re.compile(r"read_scope|write_scope|persist_conversation")
    hits = [
        p.name
        for p in src.rglob("*.py")
        if p.name != "loader.py" and pattern.search(p.read_text())
    ]
    assert "memory_policy.py" in hits
    assert SimpleNamespace  # keep import used


# --- conversational roles keep their history (review fix) ---------------------

_CONVERSATIONAL = (
    "agent",
    "support-agent",
    "sales-agent",
    "accountant-agent",
    "operator-agent",
    "developer-agent",
)
_STATELESS = ("summary-agent", "data-agent", "orchestrator")


@pytest.mark.parametrize("role", _CONVERSATIONAL)
def test_conversational_roles_persist_the_conversation(role: str) -> None:
    import warnings

    from agents_system.harness.loader import resolve

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        definition = resolve(role)
    assert MemoryPolicy.from_mapping(definition.memory_policy).persist_conversation


@pytest.mark.parametrize("role", _STATELESS)
def test_stateless_roles_keep_opting_out(role: str) -> None:
    import warnings

    from agents_system.harness.loader import resolve

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        definition = resolve(role)
    assert not MemoryPolicy.from_mapping(definition.memory_policy).persist_conversation


async def test_support_agent_remembers_the_first_turn_on_the_second() -> None:
    import warnings

    from agents_system.harness.loader import resolve

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        definition = resolve("support-agent")
    saver = _Saver()
    equipped = EquippedRuntime(
        definition=definition,
        system_prompt="support",
        tools=(),
        denied_tools=(),
        skills=(),
    )
    agent = AgentRuntime(
        equipped,
        _Model(responses=[AIMessage(content="one"), AIMessage(content="two")]),
        checkpointer=saver,
    )
    for text in ("first", "second"):
        await agent.run_turn_with_usage(
            [HumanMessage(content=text)],
            session_id="s",
            permissions=(),
            thread_id="same-thread",
        )
    assert saver.reads > 0
    assert saver.puts > 0
