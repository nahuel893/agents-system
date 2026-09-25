"""Tests for the Tool Call Interceptor — Layer-2 execution-time enforcement (D-005/D-009).

The interceptor is the second enforcement barrier: it validates every tool call
against the EquippedRuntime's injected surface BEFORE the connector executes.
Layer 1 (injector) runs at build time; Layer 2 (interceptor) runs at call time.

D-009: intercept() is now async-native. All tests converted to async def.
Strict TDD: tests written before interceptor.py changes exist.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
import structlog

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _infer_tier(perms: list[str]) -> Any:
    """Mirror the pre-tier write:/send: heuristic so existing fixtures keep
    their original sensitivity after `_is_sensitive` becomes tier-based."""
    from agents_system.harness.registry import Tier

    if any(p.startswith(("write:", "send:")) for p in perms):
        return Tier.T2
    return Tier.T1


def _spec(name: str, perms: list[str], connector: Any = None, tier: Any = None) -> Any:
    from agents_system.harness.registry import ToolSpec

    if connector is None:

        def connector(_input: Any) -> str:
            return f"{name}_result"

    return ToolSpec(
        name=name,
        required_permissions=tuple(perms),
        connector=connector,
        tier=tier if tier is not None else _infer_tier(perms),
    )


def _runtime(tools: list[Any], deploy_grant_ceiling: Any = None) -> Any:
    """Minimal EquippedRuntime with only the tools field populated.

    issue #38: Layer-2 now bounds `current_permissions` to
    `deploy_grant_ceiling`. Defaulting it to the union of every passed
    tool's own `required_permissions` (resolved to classes) keeps every
    pre-existing test in this file -- written to vary only
    `current_permissions` -- testing exactly what it tested before; the
    ceiling dimension itself is exercised by the tests that pass an
    explicit, narrower `deploy_grant_ceiling` (see
    `test_intercept_denies_when_deploy_grant_ceiling_narrower_than_current_permissions`
    below and `tests/test_issue_38_regression.py`).
    """
    from agents_system.harness.factory import EquippedRuntime
    from agents_system.permissions import permission_registry

    if deploy_grant_ceiling is None:
        names = {name for spec in tools for name in spec.required_permissions}
        deploy_grant_ceiling = frozenset(permission_registry.resolve(n) for n in names)

    return EquippedRuntime(
        definition=None,  # type: ignore[arg-type]
        system_prompt="",
        tools=tuple(tools),
        denied_tools=(),
        skills=(),
        deploy_grant_ceiling=deploy_grant_ceiling,
    )


# ---------------------------------------------------------------------------
# 1. Non-sensitive tool in surface — executes, revalidated=False
# ---------------------------------------------------------------------------


async def test_intercept_allowed_non_sensitive_tool() -> None:
    from agents_system.harness.interceptor import CallResult, intercept

    spec = _spec("session_state", [])
    runtime = _runtime([spec])

    result = await intercept("session_state", {}, runtime)

    assert isinstance(result, CallResult)
    assert result.tool_name == "session_state"
    assert result.output == "session_state_result"
    assert result.revalidated is False


# ---------------------------------------------------------------------------
# 2. Tool NOT in surface → PolicyViolation
# ---------------------------------------------------------------------------


async def test_intercept_blocks_tool_not_in_surface() -> None:
    from agents_system.harness.interceptor import PolicyViolation, intercept

    runtime = _runtime([_spec("session_state", [])])

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept("ghost_tool", {}, runtime)

    assert exc_info.value.tool_name == "ghost_tool"


async def test_intercept_logs_call_blocked_when_not_in_surface() -> None:
    from agents_system.harness.interceptor import PolicyViolation, intercept

    runtime = _runtime([_spec("session_state", [])])

    with structlog.testing.capture_logs() as logs, pytest.raises(PolicyViolation):
        await intercept("ghost_tool", {}, runtime)

    events = [e["event"] for e in logs]
    assert "interceptor.call_blocked" in events


# ---------------------------------------------------------------------------
# 3. Sensitive tool, sufficient current_permissions → executes, revalidated=True
# ---------------------------------------------------------------------------


async def test_intercept_sensitive_tool_with_sufficient_permissions() -> None:
    from agents_system.harness.interceptor import intercept

    spec = _spec("order_writer", ["write:orders", "write:order_items"])
    runtime = _runtime([spec])

    result = await intercept(
        "order_writer",
        {"items": []},
        runtime,
        current_permissions=["write:orders", "write:order_items", "read:catalog"],
    )

    assert result.revalidated is True
    assert result.tool_name == "order_writer"


# ---------------------------------------------------------------------------
# 4. Sensitive tool, insufficient current_permissions → PolicyViolation
# ---------------------------------------------------------------------------


async def test_intercept_sensitive_tool_permission_revoked() -> None:
    from agents_system.harness.interceptor import PolicyViolation, intercept

    spec = _spec("order_writer", ["write:orders", "write:order_items"])
    runtime = _runtime([spec])

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(
            "order_writer",
            {},
            runtime,
            current_permissions=["read:catalog"],  # missing write perms
        )

    assert exc_info.value.tool_name == "order_writer"


# ---------------------------------------------------------------------------
# issue #38 — Layer-2 bounds current_permissions to deploy_grant_ceiling
# ---------------------------------------------------------------------------


async def test_intercept_denies_when_deploy_grant_ceiling_narrower_than_current_permissions() -> (
    None
):
    """The deploy grant ceiling is the hard bound: a permission
    current_permissions claims but the ceiling does not include is never
    honored (spec: 'Layer-2 revalidates against the persisted deploy grant
    ceiling (issue #38)')."""
    from agents_system.harness.interceptor import PolicyViolation, intercept
    from agents_system.permissions import permission_registry

    spec = _spec("order_writer", ["write:orders", "write:order_items"])
    # Ceiling covers only write:orders -- write:order_items is outside it,
    # even though current_permissions (below) claims both.
    runtime = _runtime(
        [spec],
        deploy_grant_ceiling=frozenset({permission_registry.resolve("write:orders")}),
    )

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(
            "order_writer",
            {},
            runtime,
            current_permissions=["write:orders", "write:order_items"],
        )

    assert exc_info.value.reason == "permission_revoked"


async def test_intercept_logs_blocked_on_permission_revoked() -> None:
    from agents_system.harness.interceptor import PolicyViolation, intercept

    spec = _spec("order_writer", ["write:orders", "write:order_items"])
    runtime = _runtime([spec])

    with structlog.testing.capture_logs() as logs, pytest.raises(PolicyViolation):
        await intercept(
            "order_writer", {}, runtime, current_permissions=["read:catalog"]
        )

    assert any(e["event"] == "interceptor.call_blocked" for e in logs)


# ---------------------------------------------------------------------------
# 5. Sensitive tool, current_permissions=None → PolicyViolation
# ---------------------------------------------------------------------------


async def test_intercept_sensitive_tool_without_permissions_raises() -> None:
    from agents_system.harness.interceptor import PolicyViolation, intercept

    spec = _spec("message_sender", ["send:message"])
    runtime = _runtime([spec])

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(
            "message_sender", {"text": "hi"}, runtime
        )  # no current_permissions

    assert exc_info.value.tool_name == "message_sender"


# ---------------------------------------------------------------------------
# 6. Non-sensitive tool — current_permissions is irrelevant, not revalidated
# ---------------------------------------------------------------------------


async def test_intercept_non_sensitive_tool_ignores_current_permissions() -> None:
    from agents_system.harness.interceptor import intercept

    spec = _spec("catalog_search", ["read:catalog"])
    runtime = _runtime([spec])

    # read:catalog is NOT sensitive (not write: or send:) — no revalidation needed
    result = await intercept(
        "catalog_search",
        {"q": "sugar"},
        runtime,
        current_permissions=["read:catalog"],
    )

    assert result.revalidated is False


# ---------------------------------------------------------------------------
# 7. Structured events: call_allowed and call_executed on success
# ---------------------------------------------------------------------------


async def test_intercept_logs_call_allowed_and_executed_on_success() -> None:
    from agents_system.harness.interceptor import intercept

    spec = _spec("session_state", [])
    runtime = _runtime([spec])

    with structlog.testing.capture_logs() as logs:
        await intercept("session_state", {}, runtime)

    events = [e["event"] for e in logs]
    assert "interceptor.call_allowed" in events
    assert "interceptor.call_executed" in events


# ---------------------------------------------------------------------------
# D-009 RED tests — async connector dispatch (Phase 1)
# ---------------------------------------------------------------------------


async def test_async_connector_dispatched_and_awaited() -> None:
    """Async connector is awaited directly; session kwarg is forwarded."""
    from agents_system.harness.interceptor import CallResult, intercept
    from agents_system.harness.registry import Tier, ToolSpec

    received_session: list[Any] = []

    async def fake_async_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        received_session.append(session)
        return {"async": True}

    spec = ToolSpec(
        name="async_tool",
        required_permissions=(),
        connector=fake_async_connector,
        tier=Tier.T0,
    )
    runtime = _runtime([spec])
    sentinel = object()

    result = await intercept("async_tool", {}, runtime, session=sentinel)

    assert isinstance(result, CallResult)
    assert result.tool_name == "async_tool"
    assert result.output == {"async": True}
    assert len(received_session) == 1
    assert received_session[0] is sentinel


async def test_policy_violation_raised_for_async_connector() -> None:
    """Enforcement (surface check) fires before async connector runs."""
    from agents_system.harness.interceptor import PolicyViolation, intercept
    from agents_system.harness.registry import Tier, ToolSpec

    called: list[bool] = []

    async def sensitive_async_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> dict[str, Any]:
        called.append(True)
        return {"data": "secret"}

    spec = ToolSpec(
        name="secure_async_tool",
        required_permissions=("send:message",),
        connector=sensitive_async_connector,
        tier=Tier.T2,
    )
    runtime = _runtime([spec])

    with pytest.raises(PolicyViolation) as exc_info:
        # Not passing current_permissions → revalidation_required
        await intercept("secure_async_tool", {}, runtime)

    assert exc_info.value.tool_name == "secure_async_tool"
    assert not called, "Connector must not run before policy check passes"


# ---------------------------------------------------------------------------
# D-014 S3 — always_revalidate opt-in for sensitive reads (design AD-4b)
# ---------------------------------------------------------------------------


async def test_always_revalidate_read_blocked_when_permission_missing() -> None:
    """An always_revalidate=True read tool is revalidated like write:/send:."""
    from agents_system.harness.interceptor import PolicyViolation, intercept
    from agents_system.harness.registry import Tier, ToolSpec

    def connector(_input: Any) -> str:
        return "sensitive_result"

    spec = ToolSpec(
        name="sensitive_read",
        required_permissions=("read:client_registry",),
        connector=connector,
        always_revalidate=True,
        tier=Tier.T1,
    )
    runtime = _runtime([spec])

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(
            "sensitive_read",
            {},
            runtime,
            current_permissions=["read:catalog"],  # missing read:client_registry
        )

    assert exc_info.value.tool_name == "sensitive_read"
    assert exc_info.value.reason == "permission_revoked"


async def test_always_revalidate_read_allowed_when_permission_present() -> None:
    """An always_revalidate=True read tool executes when the permission is present."""
    from agents_system.harness.interceptor import intercept
    from agents_system.harness.registry import Tier, ToolSpec

    def connector(_input: Any) -> str:
        return "sensitive_result"

    spec = ToolSpec(
        name="sensitive_read",
        required_permissions=("read:client_registry",),
        connector=connector,
        always_revalidate=True,
        tier=Tier.T1,
    )
    runtime = _runtime([spec])

    result = await intercept(
        "sensitive_read",
        {},
        runtime,
        current_permissions=["read:client_registry"],
    )

    assert result.revalidated is True
    assert result.output == "sensitive_result"


async def test_unflagged_read_proceeds_regardless_of_current_permissions() -> None:
    """Regression guard: a read tool with always_revalidate=False (default) is
    unaffected by current_permissions content — existing behavior unchanged."""
    from agents_system.harness.interceptor import intercept
    from agents_system.harness.registry import Tier, ToolSpec

    def connector(_input: Any) -> str:
        return "catalog_result"

    spec = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=connector,
        tier=Tier.T1,
    )
    runtime = _runtime([spec])

    result = await intercept(
        "catalog_search",
        {},
        runtime,
        current_permissions=[],  # empty/irrelevant — not sensitive, not flagged
    )

    assert result.revalidated is False
    assert result.output == "catalog_result"


def test_connector_no_commit_rollback() -> None:
    """Static assertion: no connector in src/agents_system/connectors/ calls commit() or rollback()."""
    connectors_dir = (
        Path(__file__).parent.parent / "src" / "agents_system" / "connectors"
    )
    py_files = list(connectors_dir.glob("*.py"))
    assert py_files, "No connector files found — check the path"

    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in (
                    "commit",
                    "rollback",
                ):
                    raise AssertionError(
                        f"Connector file {py_file.name} calls "
                        f"'{func.attr}()' at line {node.lineno}. "
                        "Connectors must NOT manage transactions."
                    )
