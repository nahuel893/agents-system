"""Tests for the Tool Call Interceptor — Layer-2 execution-time enforcement (D-005).

The interceptor is the second enforcement barrier: it validates every tool call
against the EquippedRuntime's injected surface BEFORE the connector executes.
Layer 1 (injector) runs at build time; Layer 2 (interceptor) runs at call time.

Strict TDD: tests written before interceptor.py exists.
"""
from __future__ import annotations

from typing import Any

import pytest
import structlog


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _spec(name: str, perms: list[str], connector: Any = None) -> Any:
    from agentsys.harness.registry import ToolSpec

    if connector is None:
        connector = lambda _input: f"{name}_result"
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

def test_intercept_allowed_non_sensitive_tool() -> None:
    from agentsys.harness.interceptor import CallResult, intercept

    spec = _spec("session_state", [])
    runtime = _runtime([spec])

    result = intercept("session_state", {}, runtime)

    assert isinstance(result, CallResult)
    assert result.tool_name == "session_state"
    assert result.output == "session_state_result"
    assert result.revalidated is False


# ---------------------------------------------------------------------------
# 2. Tool NOT in surface → PolicyViolation
# ---------------------------------------------------------------------------

def test_intercept_blocks_tool_not_in_surface() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    runtime = _runtime([_spec("session_state", [])])

    with pytest.raises(PolicyViolation) as exc_info:
        intercept("ghost_tool", {}, runtime)

    assert exc_info.value.tool_name == "ghost_tool"


def test_intercept_logs_call_blocked_when_not_in_surface() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    runtime = _runtime([_spec("session_state", [])])

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(PolicyViolation):
            intercept("ghost_tool", {}, runtime)

    events = [e["event"] for e in logs]
    assert "interceptor.call_blocked" in events


# ---------------------------------------------------------------------------
# 3. Sensitive tool, sufficient current_permissions → executes, revalidated=True
# ---------------------------------------------------------------------------

def test_intercept_sensitive_tool_with_sufficient_permissions() -> None:
    from agentsys.harness.interceptor import CallResult, intercept

    spec = _spec("order_writer", ["write:orders", "write:order_items"])
    runtime = _runtime([spec])

    result = intercept(
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

def test_intercept_sensitive_tool_permission_revoked() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    spec = _spec("order_writer", ["write:orders", "write:order_items"])
    runtime = _runtime([spec])

    with pytest.raises(PolicyViolation) as exc_info:
        intercept(
            "order_writer",
            {},
            runtime,
            current_permissions=["read:catalog"],  # missing write perms
        )

    assert exc_info.value.tool_name == "order_writer"


def test_intercept_logs_blocked_on_permission_revoked() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    spec = _spec("order_writer", ["write:orders", "write:order_items"])
    runtime = _runtime([spec])

    with structlog.testing.capture_logs() as logs:
        with pytest.raises(PolicyViolation):
            intercept("order_writer", {}, runtime, current_permissions=["read:catalog"])

    assert any(e["event"] == "interceptor.call_blocked" for e in logs)


# ---------------------------------------------------------------------------
# 5. Sensitive tool, current_permissions=None → PolicyViolation
# ---------------------------------------------------------------------------

def test_intercept_sensitive_tool_without_permissions_raises() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    spec = _spec("message_sender", ["send:message"])
    runtime = _runtime([spec])

    with pytest.raises(PolicyViolation) as exc_info:
        intercept("message_sender", {"text": "hi"}, runtime)  # no current_permissions

    assert exc_info.value.tool_name == "message_sender"


# ---------------------------------------------------------------------------
# 6. Non-sensitive tool — current_permissions is irrelevant, not revalidated
# ---------------------------------------------------------------------------

def test_intercept_non_sensitive_tool_ignores_current_permissions() -> None:
    from agentsys.harness.interceptor import CallResult, intercept

    spec = _spec("catalog_search", ["read:catalog"])
    runtime = _runtime([spec])

    # read:catalog is NOT sensitive (not write: or send:) — no revalidation needed
    result = intercept(
        "catalog_search",
        {"q": "sugar"},
        runtime,
        current_permissions=["read:catalog"],
    )

    assert result.revalidated is False


# ---------------------------------------------------------------------------
# 7. Structured events: call_allowed and call_executed on success
# ---------------------------------------------------------------------------

def test_intercept_logs_call_allowed_and_executed_on_success() -> None:
    from agentsys.harness.interceptor import intercept

    spec = _spec("session_state", [])
    runtime = _runtime([spec])

    with structlog.testing.capture_logs() as logs:
        intercept("session_state", {}, runtime)

    events = [e["event"] for e in logs]
    assert "interceptor.call_allowed" in events
    assert "interceptor.call_executed" in events
