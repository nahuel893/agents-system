from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol


class Tier(str, Enum):
    """Capability tier (ADR-002 C.10): how dangerous a tool IS, independent
    of how its permission happens to be named.

    Replaces inferring sensitivity solely from the ``write:``/``send:``
    permission-prefix heuristic — a tool author who forgets to prefix a
    dangerous permission with ``exec:`` was previously not caught by
    anything. Tier is an explicit, reviewable classification per tool.

    ==== ======================================== ==============================
    T0   Inherent — every agent needs it to        session read
         function at all
    T1   Scoped read                               knowledge base lookup,
                                                     sales report read
    T2   Scoped write/send — always revalidated    order writer, ``send:message``
         at call time
    T3   Host execution — ``operator-agent``       ``use_term``, ``read_file``
         branch only
    ==== ======================================== ==============================
    """

    T0 = "T0"
    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


#: Permission-family prefixes whose danger a tier MUST reflect, regardless of
#: how carefully (or carelessly) a tool author classified the tool by hand.
#: Case-insensitive, whitespace-tolerant — mirrors
#: ``loader._is_exec_permission``'s own tolerance, for the same reason: a
#: stray case or whitespace variant is still, functionally, the same
#: permission family and must not silently disarm the guard.
_WRITE_SEND_PREFIXES = ("write:", "send:")
_EXEC_PREFIX = "exec:"


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
    tier: Tier
    """Capability tier (ADR-002 C.10). Required, no default — a tool author
    must make an explicit, reviewable classification for every tool; there is
    no "safe" default tier a forgotten call site would silently fall back to.
    See ``Tier`` for the T0-T3 definitions.
    """
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    always_revalidate: bool = False
    """Opt-in flag marking a read tool as requiring call-time revalidation.

    Tier T2/T3 tools are already always revalidated (see
    ``interceptor._is_sensitive``). Setting this to True extends the same
    revalidation to a specific T0/T1 read tool without reclassifying it.
    Defaults to False — zero behavior change for existing tools.
    """

    def __post_init__(self) -> None:
        """Fail closed at construction (ADR-002 C.10 review follow-up,
        PR #142): replacing the write:/send: prefix heuristic with tier-based
        revalidation must not turn "every tool is revalidated correctly"
        into "every tool author must remember to self-classify correctly".
        A permission whose family implies danger must be paired with a tier
        that actually gets revalidated — checked here, at registration time,
        so a mismatched tier fails loudly for every caller (production
        builders AND ad hoc test fixtures), not only for the tools this
        module happens to construct itself.
        """
        for permission in self.required_permissions:
            normalized = permission.strip().lower()
            if normalized.startswith(_EXEC_PREFIX):
                if self.tier is not Tier.T3:
                    raise ValueError(
                        f"ToolSpec {self.name!r}: permission {permission!r} is "
                        f"in the exec:* family, which requires tier=T3 (host "
                        f"execution, ADR-002 C.10) — got tier={self.tier.value}."
                    )
            elif normalized.startswith(_WRITE_SEND_PREFIXES):
                if self.tier not in (Tier.T2, Tier.T3):
                    raise ValueError(
                        f"ToolSpec {self.name!r}: permission {permission!r} is "
                        f"a write:/send: permission, which requires tier in "
                        f"{{T2, T3}} (ADR-002 C.10) — got tier={self.tier.value}."
                    )

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


class RegistryFactory(Protocol):
    """Builds the tool registry an application boots with.

    The platform assembles roles against a registry but populates none: a
    connector binds a tool name to one deployment's data, so the mapping is
    the caller's. `main.create_app` takes one of these and the lifespan calls
    it once the settings, embedder and BI engine are resolved.

    `bi_engine` is None when no read-only reporting connection is
    configured; a factory that registers `run_report` must still register it
    unbound, because a tool a manifest names but the registry lacks makes
    the whole role unbuildable rather than partially usable.
    """

    def __call__(
        self,
        settings: Any,
        embedder: Any = None,
        bi_engine: Any = None,
    ) -> ToolRegistry:
        ...
