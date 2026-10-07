"""Subject-scoped tools and the scope handle (ADR-011 parts 1 and 3, #256).

A tool never holds a subject key: the interceptor hands a subject-scoped
connector a `scope=` handle bound to (runtime id, subject key), and the
backend behind the handle fences the rows. These tests pin the interceptor
contract and the in-process reference backend; the Postgres backend is proven
in `tests/test_subject_scope_postgres.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from agents_system.harness.interceptor import PolicyViolation, intercept
from agents_system.harness.registry import Tier, ToolSpec
from agents_system.scope import (
    InMemoryScopeProvider,
    assert_subject_isolation,
)
from agents_system.subject import TurnActor, TurnSubject, turn_scope


def _runtime(*tools: ToolSpec) -> Any:
    from agents_system.harness.factory import EquippedRuntime
    from agents_system.permissions import permission_registry

    names = {n for t in tools for n in t.required_permissions}
    return EquippedRuntime(
        definition=None,  # type: ignore[arg-type]
        system_prompt="",
        tools=tuple(tools),
        denied_tools=(),
        skills=(),
        deploy_grant_ceiling=frozenset(permission_registry.resolve(n) for n in names),
    )


def _scoped_tool(seen: list[Any]) -> ToolSpec:
    async def connector(inputs: dict, *, session: Any = None, scope: Any) -> dict:
        seen.append(scope)
        scope.put("notes", inputs["k"], inputs["v"])
        return {"rows": scope.rows("notes")}

    return ToolSpec(
        name="notes",
        required_permissions=(),
        connector=connector,
        tier=Tier.T1,
        subject_scoped=True,
    )


def test_subject_scoped_defaults_to_false() -> None:
    spec = ToolSpec(
        name="t", required_permissions=(), connector=lambda i: i, tier=Tier.T1
    )
    assert spec.subject_scoped is False


async def test_reference_backend_unfiltered_read_sees_only_own_rows() -> None:
    provider = InMemoryScopeProvider()
    a = provider.scope("rt", "participant:a")
    b = provider.scope("rt", "participant:b")
    a.put("notes", "1", "a-secret")
    b.put("notes", "1", "b-secret")

    assert a.rows("notes") == {"1": "a-secret"}
    assert b.rows("notes") == {"1": "b-secret"}
    # A different runtime id with the same subject is a different partition.
    assert provider.scope("other", "participant:a").rows("notes") == {}


async def test_structural_helper_passes_for_reference_backend() -> None:
    provider = InMemoryScopeProvider()

    async def write(scope: Any, value: str) -> None:
        scope.put("notes", "1", value)

    async def read_all(scope: Any) -> list[str]:
        return list(scope.rows("notes").values())

    await assert_subject_isolation(provider, write=write, read_all=read_all)


async def test_structural_helper_fails_for_a_leaking_backend() -> None:
    class Leaky(InMemoryScopeProvider):
        def scope(self, runtime_id: str, subject_key: str) -> Any:
            return super().scope(runtime_id, "shared")

    async def write(scope: Any, value: str) -> None:
        scope.put("notes", value, value)

    async def read_all(scope: Any) -> list[str]:
        return list(scope.rows("notes").values())

    with pytest.raises(AssertionError):
        await assert_subject_isolation(Leaky(), write=write, read_all=read_all)


async def test_interceptor_passes_scope_bound_to_runtime_and_subject() -> None:
    seen: list[Any] = []
    provider = InMemoryScopeProvider()
    runtime = _runtime(_scoped_tool(seen))

    with turn_scope(TurnSubject(key="participant:a"), TurnActor("library", "t")):
        result = await intercept(
            "notes",
            {"k": "1", "v": "x"},
            runtime,
            scope_provider=provider,
            runtime_id="rt",
        )

    assert result.output == {"rows": {"1": "x"}}
    assert (seen[0].runtime_id, seen[0].subject_key) == ("rt", "participant:a")


async def test_scoped_tool_without_subject_is_refused_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents_system.harness import interceptor

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(
        interceptor, "_emit", lambda name, **kw: emitted.append({"name": name, **kw})
    )
    seen: list[Any] = []
    runtime = _runtime(_scoped_tool(seen))

    with pytest.raises(PolicyViolation) as exc:
        await intercept(
            "notes",
            {"k": "1", "v": "x"},
            runtime,
            scope_provider=InMemoryScopeProvider(),
            runtime_id="rt",
        )

    assert exc.value.reason == "subject_required"
    assert seen == []  # the connector never ran, not against a default either
    assert any(
        e["name"] == "record_tool_call_blocked" and e["reason"] == "subject_required"
        for e in emitted
    )


async def test_scoped_tool_without_a_scope_provider_is_refused() -> None:
    runtime = _runtime(_scoped_tool([]))
    with (
        turn_scope(TurnSubject(key="participant:a"), None),
        pytest.raises(PolicyViolation) as exc,
    ):
        await intercept("notes", {"k": "1", "v": "x"}, runtime)
    assert exc.value.reason == "scope_unavailable"


async def test_unscoped_connector_never_receives_scope() -> None:
    received: dict[str, Any] = {}

    async def connector(inputs: dict, *, session: Any = None) -> dict:
        received["ok"] = True
        return {}

    spec = ToolSpec(
        name="plain", required_permissions=(), connector=connector, tier=Tier.T1
    )
    await intercept(
        "plain",
        {},
        _runtime(spec),
        scope_provider=InMemoryScopeProvider(),
        runtime_id="rt",
    )
    assert received == {"ok": True}


def test_runtime_with_a_scope_provider_needs_an_explicit_runtime_id() -> None:
    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from agents_system.agent.graph import AgentRuntime

    equipped = _runtime()
    model = FakeListChatModel(responses=["hi"])
    with pytest.raises(ValueError, match="runtime_id"):
        AgentRuntime(equipped, model=model, scope_provider=InMemoryScopeProvider())
    AgentRuntime(
        equipped,
        model=model,
        scope_provider=InMemoryScopeProvider(),
        runtime_id="rt",
    )


async def test_boot_helper_verifies_the_provider_only_when_a_feature_is_on() -> None:
    from agents_system.main import _start_scope_provider

    engine = object()
    # No per-user feature configured: nothing is built, nothing is checked.
    assert await _start_scope_provider(None, engine) is None  # type: ignore[arg-type]

    calls: list[Any] = []

    class Strict(InMemoryScopeProvider):
        async def verify(self) -> None:
            calls.append("verify")
            raise RuntimeError("superuser")

    with pytest.raises(RuntimeError, match="superuser"):
        await _start_scope_provider(lambda e: Strict(), engine)  # type: ignore[arg-type]
    assert calls == ["verify"]

    ok = InMemoryScopeProvider()
    assert await _start_scope_provider(lambda e: ok, engine) is ok  # type: ignore[arg-type]
