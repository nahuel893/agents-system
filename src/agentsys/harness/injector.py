from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from agentsys.harness.loader import AgentDefinition
from agentsys.harness.registry import ToolRegistry, ToolSpec


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
            raise InjectionError(f"Unknown tool: {name}")

        spec = registry.get(name)
        if set(spec.required_permissions) <= effective:
            granted.append(spec)
            continue

        missing = sorted(set(spec.required_permissions) - effective)
        denied.append((name, f"missing permissions: {', '.join(missing)}"))

    return InjectionResult(granted=tuple(granted), denied=tuple(denied))
