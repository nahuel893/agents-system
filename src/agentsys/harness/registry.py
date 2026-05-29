from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    required_permissions: tuple[str, ...]
    connector: Callable[..., Any]


class ToolNotFoundError(Exception):
    pass


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as error:
            raise ToolNotFoundError(name) from error

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs

    def names(self) -> tuple[str, ...]:
        return tuple(self._specs)
