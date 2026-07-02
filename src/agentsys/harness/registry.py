from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class ToolSpec:
    name: str
    required_permissions: tuple[str, ...]
    connector: Callable[..., Any]
    """The callable that executes the tool.

    Connector contract (D-009):
    - Sync:  ``def connector(inputs: dict, /) -> dict``
             Executed via ``asyncio.to_thread`` — must not touch async objects.
    - Async: ``async def connector(inputs: dict, *, session: AsyncSession | None = None) -> dict``
             Awaited directly; receives the turn-scoped SQLAlchemy AsyncSession
             (or None when no session_provider is configured).

    Connectors MUST NOT call ``session.commit()`` or ``session.rollback()``.
    Transaction management belongs to the orchestrator (webhook / request handler).
    """
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    always_revalidate: bool = False
    """Opt-in flag marking a read tool as requiring call-time revalidation.

    Tools whose ``required_permissions`` start with ``write:``/``send:`` are
    already always revalidated (see ``interceptor._is_sensitive``). Setting
    this to True extends the same revalidation to a specific read tool
    without a blanket prefix rule. Defaults to False — zero behavior change
    for existing tools.
    """

    def to_langchain_tool_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


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
