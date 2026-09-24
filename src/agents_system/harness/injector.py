from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import asyncio
from typing import Any

import structlog

from agents_system.connectors.command_tools import build_command_tool_specs
from agents_system.harness.loader import AgentDefinition
from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec

logger = structlog.get_logger()


async def _emit_async(recorder_name: str, **kwargs: Any) -> None:
    """Fire-and-forget audit event — never blocks the caller."""
    try:
        from agents_system.audit import recorder
        from agents_system.audit.sink import AuditSink

        fn = getattr(recorder, recorder_name, None)
        if fn is None:
            return
        import inspect

        if inspect.iscoroutinefunction(fn):
            event = await fn(**kwargs)
        else:
            event = fn(**kwargs)
        if event is not None:
            await AuditSink.current().record(event)
    except Exception:
        logger.debug("audit.emit_failed", recorder=recorder_name, exc_info=True)


_pending_emits: set["asyncio.Task[None]"] = set()


def _emit(recorder_name: str, **kwargs: Any) -> None:
    """Fire-and-forget audit event — never blocks the caller, never raises.

    ``_emit_async`` is a coroutine, and its callers are a mix of sync
    (``resolve_tool_surface``, ``build_runtime``, ``_load_skills``) and async
    (``intercept``). Every one of them called it bare, which builds a coroutine
    object and discards it before the body runs: the whole audit wiring emitted
    nothing, and the only evidence was a RuntimeWarning that does not fail a
    test run. Scheduling the coroutine on the running loop is what makes
    fire-and-forget actually fire.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop — synchronous unit tests, CLI entry points. There is
        # nothing to schedule onto, and that is not an error.
        logger.debug("audit.emit_skipped_no_loop", recorder=recorder_name)
        return

    task = loop.create_task(_emit_async(recorder_name, **kwargs))
    # create_task holds only a weak reference: without this the task can be
    # garbage collected mid-flight and the event lost.
    _pending_emits.add(task)
    task.add_done_callback(_pending_emits.discard)


class InjectionError(Exception):
    pass


@dataclass(frozen=True)
class InjectionResult:
    granted: tuple[ToolSpec, ...]
    denied: tuple[tuple[str, str], ...]


def _deny_reason(
    spec: ToolSpec, definition: AgentDefinition, effective: set[str]
) -> str | None:
    """The grant/deny decision for one `ToolSpec` — `None` means grant.

    Shared by the registry-backed loop in `resolve_tool_surface` and by
    `resolve_command_tool_surface` (ADR-002 C.12), so the untrusted_input+T3
    second barrier (ADR-002 C.10) and the permission-subset check are ONE
    tested code path for every tool surface, not two that could drift.
    """
    if definition.untrusted_input and spec.tier == Tier.T3:
        # ADR-002 C.10 — second barrier, independent of C.11's exec:*
        # invariant: an untrusted_input role must never receive a T3 tool,
        # even if its permission happens to be granted and carries no exec:
        # prefix (i.e. even where the permission-name heuristic alone would
        # have missed it).
        return (
            "tier T3 tools are never granted to an untrusted_input role (ADR-002 C.10)"
        )
    if set(spec.required_permissions) <= effective:
        return None
    missing = sorted(set(spec.required_permissions) - effective)
    return f"missing permissions: {', '.join(missing)}"


def resolve_tool_surface(
    definition: AgentDefinition,
    registry: ToolRegistry,
    granted_permissions: Iterable[str],
) -> InjectionResult:
    effective = set(definition.permissions) & set(granted_permissions)
    granted: list[ToolSpec] = []
    denied: list[tuple[str, str]] = []

    for name in definition.tools:
        if name not in registry:
            logger.error(
                "injector.unknown_tool",
                tool=name,
                role=definition.role_name,
                deployment=definition.deployment,
            )
            # D-007: record unknown_tool event before raising
            _emit(
                "record_unknown_tool",
                definition=definition,
                tool_name=name,
            )
            raise InjectionError(f"Unknown tool: {name}")

        spec = registry.get(name)
        reason = _deny_reason(spec, definition, effective)

        if reason is None:
            granted.append(spec)
            logger.info(
                "injector.tool_granted",
                tool=spec.name,
                role=definition.role_name,
                deployment=definition.deployment,
            )
            # D-007: record tool_granted event
            _emit(
                "record_tool_granted",
                definition=definition,
                tool_name=spec.name,
            )
            continue

        denied.append((name, reason))
        logger.warning(
            "injector.tool_denied",
            tool=name,
            role=definition.role_name,
            deployment=definition.deployment,
            reason=reason,
        )
        # D-007: record tool_denied event
        _emit(
            "record_tool_denied",
            definition=definition,
            tool_name=name,
            reason=reason,
        )

    logger.info(
        "injector.surface_resolved",
        role=definition.role_name,
        deployment=definition.deployment,
        granted=len(granted),
        denied=len(denied),
    )
    return InjectionResult(granted=tuple(granted), denied=tuple(denied))


def resolve_command_tool_surface(
    definition: AgentDefinition,
    granted_permissions: Iterable[str],
) -> InjectionResult:
    """The C.12 counterpart of `resolve_tool_surface`, for declarative
    `command_tools` instead of registry-backed tools.

    Command tools are per-role declarations, not entries in a shared
    `ToolRegistry` (`build_command_tool_specs` builds each one's `ToolSpec`
    fresh from the resolved `AgentDefinition`), so there is no "unknown
    tool" case here — `harness.loader` already validated every declaration
    at load time. What is left is exactly `resolve_tool_surface`'s grant
    decision, via the shared `_deny_reason` — including ADR-002 C.10's
    untrusted_input+T3 barrier, in case a command tool is itself tiered T3.
    """
    effective = set(definition.permissions) & set(granted_permissions)
    granted: list[ToolSpec] = []
    denied: list[tuple[str, str]] = []

    for spec in build_command_tool_specs(definition.command_tools):
        reason = _deny_reason(spec, definition, effective)

        if reason is None:
            granted.append(spec)
            logger.info(
                "injector.command_tool_granted",
                tool=spec.name,
                role=definition.role_name,
                deployment=definition.deployment,
            )
            _emit(
                "record_tool_granted",
                definition=definition,
                tool_name=spec.name,
            )
            continue

        denied.append((spec.name, reason))
        logger.warning(
            "injector.command_tool_denied",
            tool=spec.name,
            role=definition.role_name,
            deployment=definition.deployment,
            reason=reason,
        )
        _emit(
            "record_tool_denied",
            definition=definition,
            tool_name=spec.name,
            reason=reason,
        )

    return InjectionResult(granted=tuple(granted), denied=tuple(denied))
