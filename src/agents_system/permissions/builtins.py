"""Built-in `Permission` action families and the flat wire-name
registration table.

Spec Requirements: "Built-in action classes and their tiers", "Open
hierarchy for downstream extension". Five action roots -- `Read` (T0),
`Write` (T2), `Send` (T2), `Exec` (T3), `Run` (T2) -- plus `Spawn` (T2,
design.md Resolved Decision 2: a genuinely new top-level family classifying
the orchestrator's `spawn:*` permissions, which had no prior prefix-based
classification at all). `resource()` drives a flat registration table for
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
    """Scoped read (T0) -- every currently shipped `read:*` permission
    family, except the escalated `read:files` (see `ReadFiles` below).
    """

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
    """New top-level action family (design.md Resolved Decision 2)
    classifying the orchestrator's `spawn:sales-agent`/`spawn:data-agent`/
    `spawn:summary-agent` permissions, which had zero prefix-based
    classification before this change -- matches the spec's own
    "Declaring a new top-level action" worked example.
    """

    tier = Tier.T2


class ReadFiles(Read):
    """Escalated `read:files` classification (design.md Resolved Decision
    1): host filesystem access is genuinely T3-dangerous, unlike
    catalog/knowledge-base reads, so this subclass overrides `Read`'s T0
    with an explicit T3 escalation -- a valid R1 escalation. Registered
    directly below (not via `resource()`, since it needs a tier override
    rather than plain inheritance).
    """

    tier = Tier.T3


def resource(
    parent: type[Permission], wire_name: str, *, tier: Tier | None = None
) -> type[Permission]:
    """Create and register a resource-scoped subclass of `parent` for one
    wire name (design.md's "Built-ins Without Hand-Writing Hundreds of
    Classes"). `tier` overrides the parent's tier when given (a valid R1
    escalation); omitted, the subclass plainly inherits the parent's tier.
    """
    attrs: dict[str, Any] = {} if tier is None else {"tier": tier}
    cls = cast("type[Permission]", type(_class_name(wire_name), (parent,), attrs))
    permission_registry.register(cls, wire_name)
    return cls


def _class_name(wire_name: str) -> str:
    """Derive a readable class name from a wire name, for `__repr__`/
    debugging only -- classification never parses this string back (spec:
    "no prefix, substring, or pattern parsing of the wire name is
    permitted anywhere in the resolution path").
    """
    parts = re.split(r"[:_-]", wire_name)
    return "".join(part.capitalize() for part in parts if part) + "Permission"


#: Every shipped wire name that resolves through `resource()` -- the 11
#: `ToolSpec`-backed names from spec.md's R2a/R2b compatibility table, plus
#: the 7-name registration gap from design.md's "Full Wire-Name
#: Registration Gap" (wire names a manifest declares with no backing
#: `ToolSpec` today: `read:session`, `read:price_lists`, `write:session`,
#: `write:summary_output`, and the three `spawn:*` names). `read:files` is
#: handled separately below since it needs `ReadFiles`, not a fresh
#: `resource()`-created subclass.
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
