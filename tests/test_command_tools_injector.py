"""ADR-002 C.12 — `resolve_command_tool_surface` coverage.

Mirrors `test_harness_injector.py`'s conventions: the C.10 second barrier
(untrusted_input + T3) and the permission-subset grant/deny decision must
behave identically for declarative command tools as they already do for
registry-backed ones, because both now share `injector._deny_reason`.
"""

from __future__ import annotations

import pytest

from agents_system.harness.loader import (
    AgentDefinition,
    CommandToolDeclaration,
    CommandToolParam,
    Tier,
)
from agents_system.permissions import Run, UnknownPermissionNameError, resolve, resource


#: These fixtures build `CommandToolDeclaration`/`ToolSpec` directly,
#: bypassing `harness.loader._parse_command_tools` (which registers a
#: declared command tool's permission dynamically at manifest-parse time —
#: see that function's docstring). Register the same two wire names here so
#: this file is self-contained regardless of run order; `resource()` is not
#: idempotent (it always builds a fresh class), so check-then-create.
def _ensure_run_permission_registered(wire_name: str) -> None:
    try:
        resolve(wire_name)
    except UnknownPermissionNameError:
        resource(Run, wire_name)


_ensure_run_permission_registered("run:check_stock")
_ensure_run_permission_registered("run:dangerous_tool")


def _declaration(*, name: str, tier: Tier, permission: str) -> CommandToolDeclaration:
    return CommandToolDeclaration(
        name=name,
        argv=("/usr/bin/echo", "{sku}"),
        params={"sku": CommandToolParam(type="string")},
        tier=tier,
        permission=permission,
    )


def _definition(
    *,
    command_tools: tuple[CommandToolDeclaration, ...],
    permissions: tuple[str, ...],
    untrusted_input: bool = False,
) -> AgentDefinition:
    return AgentDefinition(
        role_name="cmdtool-role",
        version="1.0",
        deployment=None,
        system_prompt="prompt",
        tools=(),
        skills=(),
        context={},
        permissions=permissions,
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
        untrusted_input=untrusted_input,
        command_tools=command_tools,
    )


def test_command_tool_granted_when_permission_present() -> None:
    from agents_system.harness.injector import resolve_command_tool_surface

    decl = _declaration(name="check_stock", tier=Tier.T2, permission="run:check_stock")
    definition = _definition(command_tools=(decl,), permissions=("run:check_stock",))

    result = resolve_command_tool_surface(
        definition, granted_permissions=("run:check_stock",)
    )

    assert [spec.name for spec in result.granted] == ["check_stock"]
    assert result.denied == ()


def test_command_tool_denied_when_permission_missing() -> None:
    from agents_system.harness.injector import resolve_command_tool_surface

    decl = _declaration(name="check_stock", tier=Tier.T2, permission="run:check_stock")
    definition = _definition(command_tools=(decl,), permissions=("run:check_stock",))

    result = resolve_command_tool_surface(definition, granted_permissions=())

    assert result.granted == ()
    denied_names = {name for name, _reason in result.denied}
    assert denied_names == {"check_stock"}


def test_untrusted_input_role_still_receives_t2_command_tool() -> None:
    """ADR-002 C.11's acceptance criterion, at the injector layer: a
    `run:*`-permissioned, non-T3 command tool is unaffected by
    `untrusted_input`."""
    from agents_system.harness.injector import resolve_command_tool_surface

    decl = _declaration(name="check_stock", tier=Tier.T2, permission="run:check_stock")
    definition = _definition(
        command_tools=(decl,),
        permissions=("run:check_stock",),
        untrusted_input=True,
    )

    result = resolve_command_tool_surface(
        definition, granted_permissions=("run:check_stock",)
    )

    assert [spec.name for spec in result.granted] == ["check_stock"]
    assert result.denied == ()


def test_t3_command_tool_can_no_longer_be_built_at_all() -> None:
    """Superseded by permission-model Resolved Decision 3: `Run` is a single
    T2 family with no T3-floor sibling, so a T3-tiered command tool now
    fails R2b's floor the moment `resolve_command_tool_surface` tries to
    build its `ToolSpec` (`max(2) >= 3` is false) — a T3 command tool can
    no longer even reach the ADR-002 C.10 untrusted_input+T3 barrier this
    test used to exercise, regardless of `untrusted_input`. `harness.loader`
    independently rejects `tier: T3` at manifest-parse time
    (`test_command_tools_loader.py::test_parse_command_tools_t3_tier_is_now_rejected`);
    this proves the ToolSpec-level guard holds even for a declaration built
    directly, bypassing the loader."""
    from agents_system.harness.injector import resolve_command_tool_surface
    from agents_system.permissions import PermissionFloorViolationError

    decl = _declaration(
        name="dangerous_tool", tier=Tier.T3, permission="run:dangerous_tool"
    )
    definition = _definition(
        command_tools=(decl,),
        permissions=("run:dangerous_tool",),
        untrusted_input=False,
    )

    with pytest.raises(PermissionFloorViolationError, match="dangerous_tool"):
        resolve_command_tool_surface(
            definition, granted_permissions=("run:dangerous_tool",)
        )


# ---------------------------------------------------------------------------
# PR #147 review follow-up — a T2 command tool must stay equipped for an
# untrusted_input role (ADR-002 C.12's own worked example) AND must be
# revalidated by the Layer-2 interceptor at call time, exactly like any
# other T2/T3 tool (`interceptor._is_sensitive`). Proving this closes the
# gap a T0 command tool left open: T0 is equipped AND never revalidated.
# ---------------------------------------------------------------------------
async def test_t2_command_tool_is_equipped_and_revalidated_by_interceptor() -> None:
    from agents_system.connectors.command_tools import build_command_tool_spec
    from agents_system.harness.factory import EquippedRuntime
    from agents_system.harness.injector import resolve_command_tool_surface
    from agents_system.harness.interceptor import PolicyViolation, intercept

    decl = _declaration(name="check_stock", tier=Tier.T2, permission="run:check_stock")
    definition = _definition(
        command_tools=(decl,),
        permissions=("run:check_stock",),
        untrusted_input=True,
    )

    # Equipped: an untrusted_input role still gets its narrow T2 command tool.
    result = resolve_command_tool_surface(
        definition, granted_permissions=("run:check_stock",)
    )
    assert [spec.name for spec in result.granted] == ["check_stock"]

    # Revalidated: calling it with no `current_permissions` must be refused,
    # exactly like every other T2/T3 tool — proving Layer 2 does not skip it.
    spec = build_command_tool_spec(decl)
    runtime = EquippedRuntime(
        definition=None,  # type: ignore[arg-type]
        system_prompt="",
        tools=(spec,),
        denied_tools=(),
        skills=(),
    )

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept("check_stock", {"sku": "SKU1"}, runtime)

    assert exc_info.value.reason == "revalidation_required"
