"""Tool Call Interceptor — Layer-2 execution-time enforcement.

Layer 1 (injector/factory) runs at build time and restricts which tools the
model can see. Layer 2 (this module) runs at call time and validates every
invocation before the connector fires.

This catches what Layer 1 cannot: model hallucinations of out-of-scope tools,
prompt injection attempts, and incomplete injection bugs.

Sensitive tools — those whose required_permissions include any write:* or
send:* permission, OR whose ToolSpec opts in via ``always_revalidate=True`` —
are revalidated against current permissions at call time. This guards against
permission changes that occur between runtime instantiation and the actual
tool invocation in long-running sessions. ``always_revalidate`` lets specific
read tools opt into the same revalidation without a blanket prefix rule.

D-009: intercept() is async-native. Async connectors are awaited directly;
sync connectors are offloaded via asyncio.to_thread so the event loop stays
free. Policy enforcement remains synchronous and runs before dispatch.
"""
from __future__ import annotations

import asyncio
import dataclasses
from typing import Any, Iterable

import structlog

from agentsys.harness.factory import EquippedRuntime
from agentsys.harness.registry import ToolSpec

logger = structlog.get_logger()

_SENSITIVE_PREFIXES = ("write:", "send:")


def _is_sensitive(spec: ToolSpec) -> bool:
    return (
        any(perm.startswith(_SENSITIVE_PREFIXES) for perm in spec.required_permissions)
        or spec.always_revalidate
    )


class PolicyViolation(Exception):
    """Raised when a tool call is blocked by the interceptor."""

    def __init__(self, tool_name: str, reason: str) -> None:
        super().__init__(f"Tool call blocked — tool={tool_name!r} reason={reason}")
        self.tool_name = tool_name
        self.reason = reason


@dataclasses.dataclass(frozen=True)
class CallResult:
    """The outcome of a successful (permitted) tool call."""

    tool_name: str
    output: Any
    revalidated: bool


async def intercept(
    tool_name: str,
    tool_input: dict[str, Any],
    runtime: EquippedRuntime,
    *,
    current_permissions: Iterable[str] | None = None,
    session: Any = None,
) -> CallResult:
    """Validate and execute a tool call against the injected surface.

    Parameters
    ----------
    tool_name:
        The tool the model requested.
    tool_input:
        The arguments to pass to the connector.
    runtime:
        The equipped runtime whose injected surface is the authority.
    current_permissions:
        The caller's current permission grants, used to revalidate sensitive
        tools at execution time. Must be provided for any sensitive tool.
    session:
        An optional SQLAlchemy AsyncSession opened per-turn by the caller.
        Forwarded as a keyword argument to async connectors only. Sync
        connectors do not receive it (they run in asyncio.to_thread and must
        not access async session objects from a thread context).
    """
    surface: dict[str, ToolSpec] = {t.name: t for t in runtime.tools}

    if tool_name not in surface:
        logger.warning(
            "interceptor.call_blocked",
            tool=tool_name,
            reason="not_in_surface",
        )
        raise PolicyViolation(tool_name, "not_in_surface")

    spec = surface[tool_name]
    sensitive = _is_sensitive(spec)
    revalidated = False

    if sensitive:
        if current_permissions is None:
            logger.warning(
                "interceptor.call_blocked",
                tool=tool_name,
                reason="revalidation_required",
            )
            raise PolicyViolation(tool_name, "revalidation_required")

        effective = set(current_permissions)
        if not set(spec.required_permissions) <= effective:
            logger.warning(
                "interceptor.call_blocked",
                tool=tool_name,
                reason="permission_revoked",
            )
            raise PolicyViolation(tool_name, "permission_revoked")

        revalidated = True

    logger.info("interceptor.call_allowed", tool=tool_name, sensitive=sensitive)

    if asyncio.iscoroutinefunction(spec.connector):
        output = await spec.connector(tool_input, session=session)
    else:
        output = await asyncio.to_thread(spec.connector, tool_input)

    logger.info("interceptor.call_executed", tool=tool_name)

    return CallResult(tool_name=tool_name, output=output, revalidated=revalidated)
