from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import structlog

from agentsys.harness.loader import AgentDefinition
from agentsys.harness.registry import ToolRegistry, ToolSpec

logger = structlog.get_logger()


async def _emit_async(recorder_name: str, **kwargs) -> None:
    """Fire-and-forget audit event — never blocks the caller."""
    try:
        from agentsys.audit import recorder
        from agentsys.audit.sink import AuditSink

        fn = getattr(recorder, recorder_name, None)
        if fn is None:
            return
        import inspect

        sig = inspect.signature(fn)
        if inspect.iscoroutinefunction(fn):
            event = await fn(**kwargs)
        else:
            event = fn(**kwargs)
        if event is not None:
            await AuditSink.current().record(event)
    except Exception:
        pass  # Never let audit failure propagate


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
            _emit_async(
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
            _emit_async(
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
        _emit_async(
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
