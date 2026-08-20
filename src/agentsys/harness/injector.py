from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import asyncio
from typing import Any

import structlog

from agentsys.harness.loader import AgentDefinition
from agentsys.harness.registry import ToolRegistry, ToolSpec

logger = structlog.get_logger()


async def _emit_async(recorder_name: str, **kwargs: Any) -> None:
    """Fire-and-forget audit event — never blocks the caller."""
    try:
        from agentsys.audit import recorder
        from agentsys.audit.sink import AuditSink

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
        if set(spec.required_permissions) <= effective:
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

        missing = sorted(set(spec.required_permissions) - effective)
        reason = f"missing permissions: {', '.join(missing)}"
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
