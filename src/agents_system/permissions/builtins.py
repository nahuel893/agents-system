"""Built-in `Permission` action families and the flat wire-name
registration table (spec: "Built-in action classes and their tiers", "Open
hierarchy for downstream extension"). `resource()` drives registration for
every shipped wire name instead of one hand-written class per row
(design.md's "Built-ins Without Hand-Writing Hundreds of Classes").
"""

from __future__ import annotations

import re
from typing import Any, cast

from agents_system.harness.registry import Tier

from .base import Permission
from .permission_registry import permission_registry


class Read(Permission):
    """Scoped read (T0), except the escalated `read:files` (`ReadFiles`)."""

    tier = Tier.T0


class Write(Permission):
    """Scoped write, always revalidated at call time (T2)."""

    tier = Tier.T2


class Send(Permission):
    """Scoped send, always revalidated at call time (T2)."""

    tier = Tier.T2


class Exec(Permission):
    """Host execution (T3) -- `operator-agent` branch only."""

    tier = Tier.T3


class Run(Permission):
    """Declarative command-tool execution (T2)."""

    tier = Tier.T2


class Spawn(Permission):
    """New top-level action family classifying the orchestrator's
    `spawn:*` permissions (design.md Resolved Decision 2).
    """

    tier = Tier.T2


class ReadFiles(Read):
    """Escalated `read:files` classification: host filesystem access is
    T3-dangerous (design.md Resolved Decision 1, a valid R1 escalation).
    """

    tier = Tier.T3


def resource(
    parent: type[Permission], wire_name: str, *, tier: Tier | None = None
) -> type[Permission]:
    """Create and register a resource-scoped subclass of `parent` for one
    wire name. `tier` overrides the parent's tier when given.
    """
    attrs: dict[str, Any] = {} if tier is None else {"tier": tier}
    cls = cast("type[Permission]", type(_class_name(wire_name), (parent,), attrs))
    permission_registry.register(cls, wire_name)
    return cls


def _class_name(wire_name: str) -> str:
    """Readable class name for a wire name, `__repr__`/debugging only --
    classification never parses this string back.
    """
    parts = re.split(r"[:_-]", wire_name)
    return "".join(part.capitalize() for part in parts if part) + "Permission"


#: Every shipped wire name that resolves through `resource()` -- the 11
#: `ToolSpec`-backed names from spec.md's R2a/R2b compatibility table, plus
#: the 7-name registration gap from design.md's "Full Wire-Name
#: Registration Gap". `read:files` is handled separately below since it
#: needs `ReadFiles`, not a fresh `resource()`-created subclass.
_RESOURCE_REGISTRATIONS: tuple[tuple[type[Permission], str], ...] = (
    (Read, "read:catalog"),
    (Read, "read:client_registry"),
    (Read, "read:reports"),
    (Read, "read:knowledge_base"),
    (Read, "read:conversation_logs"),
    (Read, "read:session"),
    (Read, "read:price_lists"),
    (Send, "send:message"),
    (Send, "send:escalation"),
    (Write, "write:orders"),
    (Write, "write:order_items"),
    (Write, "write:session"),
    (Write, "write:summary_output"),
    (Exec, "exec:command"),
    (Spawn, "spawn:sales-agent"),
    (Spawn, "spawn:data-agent"),
    (Spawn, "spawn:summary-agent"),
)

for _parent, _wire_name in _RESOURCE_REGISTRATIONS:
    resource(_parent, _wire_name)

permission_registry.register(ReadFiles, "read:files")
