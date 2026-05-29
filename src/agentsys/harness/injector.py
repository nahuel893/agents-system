from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import structlog

from agentsys.harness.loader import AgentDefinition
from agentsys.harness.registry import ToolRegistry, ToolSpec

logger = structlog.get_logger()


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

    logger.info(
        "injector.surface_resolved",
        role=definition.role_name,
        deployment=definition.deployment,
        granted=len(granted),
        denied=len(denied),
    )
    return InjectionResult(granted=tuple(granted), denied=tuple(denied))
