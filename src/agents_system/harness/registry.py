from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


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
        PR #142; permission-model R2a/R2b): a permission whose class implies
        danger must be paired with a tier that actually gets revalidated —
        checked here, at construction time, so a mismatched tier fails
        loudly for every caller (production builders AND ad hoc test
        fixtures), not only for the tools this module happens to construct
        itself. The write:/send:/exec:/run: prefix heuristic that used to
        live here is gone; ``agents_system.permissions.evaluate_tool_spec``
        (deferred import — see ``harness/loader.py``'s three deferred
        ``agents_system.permissions`` imports for the identical
        cycle-avoidance reason: ``permissions.base`` imports ``Tier`` from
        this module, which initializes the ``agents_system.harness``
        package and, transitively, ``loader.py``) now resolves each
        required permission through the ``PermissionRegistry`` and checks
        the class/tier rules R2a (ceiling) and R2b (floor) directly.
        """
        from agents_system.permissions import evaluate_tool_spec  # deferred

        evaluate_tool_spec(self.name, self.tier, self.required_permissions)

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
    ) -> ToolRegistry: ...
