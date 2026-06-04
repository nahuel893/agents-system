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

def _spec(name: str, perms: list[str], connector: Any = None) -> Any:
    from agentsys.harness.registry import ToolSpec

    if connector is None:
        def connector(_input: Any) -> str:
            return f"{name}_result"
    return ToolSpec(name=name, required_permissions=tuple(perms), connector=connector)


def _runtime(tools: list[Any]) -> Any:
    """Minimal EquippedRuntime with only the tools field populated."""
    from agentsys.harness.factory import EquippedRuntime

    return EquippedRuntime(
        definition=None,  # type: ignore[arg-type]
        system_prompt="",
        tools=tuple(tools),
        denied_tools=(),
        skills=(),
    )


# ---------------------------------------------------------------------------
# 1. Non-sensitive tool in surface — executes, revalidated=False
# ---------------------------------------------------------------------------

async def test_intercept_allowed_non_sensitive_tool() -> None:
    from agentsys.harness.interceptor import CallResult, intercept

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
    from agentsys.harness.interceptor import PolicyViolation, intercept

    runtime = _runtime([_spec("session_state", [])])

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept("ghost_tool", {}, runtime)

    assert exc_info.value.tool_name == "ghost_tool"


async def test_intercept_logs_call_blocked_when_not_in_surface() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    runtime = _runtime([_spec("session_state", [])])

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(PolicyViolation):
            await intercept("ghost_tool", {}, runtime)

    events = [e["event"] for e in logs]
    assert "interceptor.call_blocked" in events


# ---------------------------------------------------------------------------
# 3. Sensitive tool, sufficient current_permissions → executes, revalidated=True
# ---------------------------------------------------------------------------

async def test_intercept_sensitive_tool_with_sufficient_permissions() -> None:
    from agentsys.harness.interceptor import intercept

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
    from agentsys.harness.interceptor import PolicyViolation, intercept

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


async def test_intercept_logs_blocked_on_permission_revoked() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    spec = _spec("order_writer", ["write:orders", "write:order_items"])
    runtime = _runtime([spec])

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(PolicyViolation):
            await intercept("order_writer", {}, runtime, current_permissions=["read:catalog"])

    assert any(e["event"] == "interceptor.call_blocked" for e in logs)


# ---------------------------------------------------------------------------
# 5. Sensitive tool, current_permissions=None → PolicyViolation
# ---------------------------------------------------------------------------

async def test_intercept_sensitive_tool_without_permissions_raises() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    spec = _spec("message_sender", ["send:message"])
    runtime = _runtime([spec])

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept("message_sender", {"text": "hi"}, runtime)  # no current_permissions

    assert exc_info.value.tool_name == "message_sender"


# ---------------------------------------------------------------------------
# 6. Non-sensitive tool — current_permissions is irrelevant, not revalidated
# ---------------------------------------------------------------------------

async def test_intercept_non_sensitive_tool_ignores_current_permissions() -> None:
    from agentsys.harness.interceptor import intercept

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
    from agentsys.harness.interceptor import intercept

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
    from agentsys.harness.interceptor import CallResult, intercept
    from agentsys.harness.registry import ToolSpec

    received_session: list[Any] = []

    async def fake_async_connector(inputs: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        received_session.append(session)
        return {"async": True}

    spec = ToolSpec(name="async_tool", required_permissions=(), connector=fake_async_connector)
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
    from agentsys.harness.interceptor import PolicyViolation, intercept
    from agentsys.harness.registry import ToolSpec

    called: list[bool] = []

    async def sensitive_async_connector(inputs: dict[str, Any], *, session: Any = None) -> dict[str, Any]:
        called.append(True)
        return {"data": "secret"}

    spec = ToolSpec(
        name="secure_async_tool",
        required_permissions=("send:message",),
        connector=sensitive_async_connector,
    )
    runtime = _runtime([spec])

    with pytest.raises(PolicyViolation) as exc_info:
        # Not passing current_permissions → revalidation_required
        await intercept("secure_async_tool", {}, runtime)

    assert exc_info.value.tool_name == "secure_async_tool"
    assert not called, "Connector must not run before policy check passes"


def test_connector_no_commit_rollback() -> None:
    """Static assertion: no connector in src/agentsys/connectors/ calls commit() or rollback()."""
    connectors_dir = (
        Path(__file__).parent.parent / "src" / "agentsys" / "connectors"
    )
    py_files = list(connectors_dir.glob("*.py"))
    assert py_files, "No connector files found — check the path"

    for py_file in py_files:
        source = py_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in ("commit", "rollback"):
                    raise AssertionError(
                        f"Connector file {py_file.name} calls "
                        f"'{func.attr}()' at line {node.lineno}. "
                        "Connectors must NOT manage transactions."
                    )
