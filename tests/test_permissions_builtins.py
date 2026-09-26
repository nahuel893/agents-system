"""Tests for the built-in `Permission` action classes and the flat
wire-name registration table.

Strict TDD: written before `agents_system.permissions.builtins` exists.
Covers spec Requirements "Built-in action classes and their tiers" and
"Open hierarchy for downstream extension".
"""

from __future__ import annotations

import pytest

from agents_system.harness.registry import Tier
from agents_system.permissions import builtins
from agents_system.permissions.base import Permission
from agents_system.permissions.permission_registry import permission_registry

# `builtins` (the permissions package's, not the stdlib module) is imported
# here, at module/collection time -- BEFORE `reset_permission_registry`
# takes its first per-test snapshot below. `builtins.py`'s registration
# table is a one-time, import-cached side effect (spec: "built-ins are
# registered once at import time"); importing it lazily inside a
# fixture-wrapped test would let that test's post-teardown `_restore()`
# wipe the registrations back out, and every later test would then see an
# empty registry (the module body never re-runs on a second import).
# Importing here means every snapshot this fixture ever takes already
# includes the built-ins.
pytestmark = [pytest.mark.usefixtures("reset_permission_registry")]

#: The 12-row R2a/R2b compatibility table in spec.md, minus non-permission
#: rows (`session_state` requires none; the public-library-example row
#: reuses `read:catalog`), plus the 6-name registration gap from design.md
#: (`spawn:*` has no prior prefix classification at all), plus `query:sql`
#: for the read-only SQL tool (#80, ADR-007) -- 19 distinct wire names
#: total.
_SHIPPED_WIRE_NAMES = (
    "read:catalog",
    "read:client_registry",
    "send:message",
    "write:orders",
    "write:order_items",
    "read:reports",
    "read:knowledge_base",
    "read:conversation_logs",
    "send:escalation",
    "exec:command",
    "read:files",
    "read:session",
    "read:price_lists",
    "write:session",
    "write:summary_output",
    "spawn:sales-agent",
    "spawn:data-agent",
    "spawn:summary-agent",
    "query:sql",
)


def test_shipped_wire_name_count_is_nineteen() -> None:
    assert len(_SHIPPED_WIRE_NAMES) == 19
    assert len(set(_SHIPPED_WIRE_NAMES)) == 19  # all distinct


@pytest.mark.parametrize(
    "action_cls_name,expected_tier",
    [
        ("Read", Tier.T0),
        ("Write", Tier.T2),
        ("Send", Tier.T2),
        ("Exec", Tier.T3),
        ("Run", Tier.T2),
        ("Spawn", Tier.T2),
        ("Query", Tier.T2),
    ],
)
def test_builtin_action_classes_exist_with_correct_tier(
    action_cls_name: str, expected_tier: Tier
) -> None:
    action_cls = getattr(builtins, action_cls_name)
    assert issubclass(action_cls, Permission)
    assert action_cls.tier is expected_tier


@pytest.mark.parametrize("wire_name", _SHIPPED_WIRE_NAMES)
def test_every_shipped_wire_name_resolves_to_a_distinct_registered_subclass(
    wire_name: str,
) -> None:
    resolved = permission_registry.resolve(wire_name)

    assert issubclass(resolved, Permission)
    # Never the bare action-root class itself (spec: "A resource-scoped wire
    # string ... MUST resolve to a specific registered subclass of its
    # action family, never to the bare generic action class").
    assert resolved not in (
        builtins.Read,
        builtins.Write,
        builtins.Send,
        builtins.Exec,
        builtins.Run,
        builtins.Spawn,
        builtins.Query,
    )


def test_all_nineteen_wire_names_resolve_to_distinct_classes() -> None:
    resolved_classes = {
        wire_name: permission_registry.resolve(wire_name)
        for wire_name in _SHIPPED_WIRE_NAMES
    }
    assert len(set(resolved_classes.values())) == len(_SHIPPED_WIRE_NAMES)


def test_downstream_subclass_of_write_registers_and_resolves_like_a_builtin() -> None:
    class ExportWrite(builtins.Write):
        tier = Tier.T2

    permission_registry.register(ExportWrite, "write:export_downstream_test")

    assert permission_registry.resolve("write:export_downstream_test") is ExportWrite
    assert issubclass(ExportWrite, builtins.Write)


def test_spawn_is_a_new_top_level_family_at_t2() -> None:
    assert issubclass(builtins.Spawn, Permission)
    assert builtins.Spawn.tier is Tier.T2
    # A genuinely new top-level action, not a subclass of any other built-in.
    assert builtins.Spawn.__mro__[1] is Permission


@pytest.mark.parametrize(
    "wire_name", ["spawn:sales-agent", "spawn:data-agent", "spawn:summary-agent"]
)
def test_spawn_wire_names_resolve_to_distinct_spawn_subclasses(wire_name: str) -> None:
    resolved = permission_registry.resolve(wire_name)

    assert issubclass(resolved, builtins.Spawn)
    assert resolved is not builtins.Spawn


def test_read_files_resolves_to_a_dedicated_escalated_subclass() -> None:
    """Resolved Decision 1: `read:files` classifies to `ReadFiles(Read)` at
    an escalated `tier = Tier.T3` (a valid R1 escalation over `Read`'s T0).
    """
    assert builtins.ReadFiles.tier is Tier.T3
    assert issubclass(builtins.ReadFiles, builtins.Read)
    assert permission_registry.resolve("read:files") is builtins.ReadFiles
