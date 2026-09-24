from __future__ import annotations

import pytest

from agents_system.harness.loader import AgentDefinition
from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec
from agents_system.permissions import Read, register


#: Test-local ancestor/descendant family for the R3 descendant-coverage
#: scenarios below -- distinct, file-scoped wire names so registration
#: never collides with another test module's own fixtures.
class _R3Ancestor(Read):
    """T0 ancestor, registered under its own wire name."""


class _R3SameTierDescendant(_R3Ancestor):
    """Same-tier (T0) subclass -- a valid R1 equal-tier descendant."""


class _R3EscalatedDescendant(_R3Ancestor):
    """Escalated to T2 -- a valid R1 escalation."""

    tier = Tier.T2


register(_R3Ancestor, "read:injector_r3_ancestor")
register(_R3SameTierDescendant, "read:injector_r3_same_tier_descendant")
register(_R3EscalatedDescendant, "read:injector_r3_escalated_descendant")


def _connector() -> str:
    return "ok"


def _definition(
    *,
    tools: tuple[str, ...],
    permissions: tuple[str, ...],
    untrusted_input: bool = False,
) -> AgentDefinition:
    return AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="prompt",
        tools=tools,
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
    )


def test_resolve_tool_surface_grants_all_tools_when_permissions_present() -> None:
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    catalog_search = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )
    order_writer = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders",),
        connector=_connector,
        tier=Tier.T2,
    )
    registry.register(catalog_search)
    registry.register(order_writer)
    definition = _definition(
        tools=("catalog_search", "order_writer"),
        permissions=("read:catalog", "write:orders"),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("read:catalog", "write:orders"),
    )

    assert result.granted == (catalog_search, order_writer)
    assert result.denied == ()


def test_resolve_tool_surface_denies_tool_with_missing_permissions() -> None:
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    catalog_search = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )
    order_writer = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders", "write:order_items"),
        connector=_connector,
        tier=Tier.T2,
    )
    registry.register(catalog_search)
    registry.register(order_writer)
    definition = _definition(
        tools=("catalog_search", "order_writer"),
        permissions=("read:catalog", "write:orders", "write:order_items"),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("read:catalog", "write:orders"),
    )

    assert result.granted == (catalog_search,)
    assert result.denied == (
        ("order_writer", "missing permissions: write:order_items"),
    )


def test_resolve_tool_surface_uses_role_and_user_permission_intersection() -> None:
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    client_lookup = ToolSpec(
        name="client_lookup",
        required_permissions=("read:client_registry",),
        connector=_connector,
        tier=Tier.T1,
    )
    registry.register(client_lookup)
    definition = _definition(
        tools=("client_lookup",),
        permissions=("read:client_registry",),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=(),
    )

    assert result.granted == ()
    assert result.denied == (
        ("client_lookup", "missing permissions: read:client_registry"),
    )


def test_resolve_tool_surface_raises_for_unregistered_tool() -> None:
    from agents_system.harness.injector import InjectionError, resolve_tool_surface

    definition = _definition(
        tools=("missing_tool",),
        permissions=("read:catalog",),
    )

    with pytest.raises(InjectionError, match="missing_tool"):
        resolve_tool_surface(
            definition,
            ToolRegistry(),
            granted_permissions=("read:catalog",),
        )


# ---------------------------------------------------------------------------
# ADR-002 C.10 — second barrier: untrusted_input roles never receive T3 tools
# ---------------------------------------------------------------------------


def test_untrusted_input_role_denied_t3_tool_even_with_permission_granted() -> None:
    """The exact scenario C.10 exists for: a T3 tool named under a `read:*`
    permission (no `exec:` prefix) that a hypothetical manifest grants to an
    `untrusted_input` role must still never reach the model's tool surface.
    `read:files` is the real, registered instance of this shape
    (`ReadFiles(Read)` at T3, PR1 Resolved Decision 1).
    """
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    disguised_t3 = ToolSpec(
        name="disguised_t3_tool",
        required_permissions=("read:files",),  # deliberately not exec:*
        connector=_connector,
        tier=Tier.T3,
    )
    registry.register(disguised_t3)
    definition = _definition(
        tools=("disguised_t3_tool",),
        permissions=("read:files",),
        untrusted_input=True,
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("read:files",),  # permission WOULD be satisfied
    )

    assert result.granted == ()
    assert result.denied == (
        (
            "disguised_t3_tool",
            "tier T3 tools are never granted to an untrusted_input role (ADR-002 C.10)",
        ),
    )


def test_untrusted_input_role_denied_real_t3_tool_by_name() -> None:
    """Same barrier, exercised against the real `use_term`/`read_file` specs."""
    from agents_system.connectors.operator import build_operator_tool_specs
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    for spec in build_operator_tool_specs(None):
        registry.register(spec)
    definition = _definition(
        tools=("use_term", "read_file"),
        permissions=("exec:command", "read:files"),
        untrusted_input=True,
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("exec:command", "read:files"),
    )

    assert result.granted == ()
    denied_names = {name for name, _reason in result.denied}
    assert denied_names == {"use_term", "read_file"}


def test_trusted_role_still_receives_t3_tool_when_permission_granted() -> None:
    """Regression: the new barrier must not overreach — a role that is NOT
    `untrusted_input` (e.g. `operator-agent`) keeps receiving its T3 tools."""
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    t3_tool = ToolSpec(
        name="use_term",
        required_permissions=("exec:command",),
        connector=_connector,
        tier=Tier.T3,
    )
    registry.register(t3_tool)
    definition = _definition(
        tools=("use_term",),
        permissions=("exec:command",),
        untrusted_input=False,
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("exec:command",),
    )

    assert result.granted == (t3_tool,)
    assert result.denied == ()


def test_untrusted_input_role_still_receives_t1_and_t2_tools() -> None:
    """Regression: the barrier is scoped to T3 only — T1/T2 tools an
    untrusted_input role legitimately holds (e.g. sales-agent's order_writer)
    are unaffected."""
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    catalog_search = ToolSpec(
        name="catalog_search",
        required_permissions=("read:catalog",),
        connector=_connector,
        tier=Tier.T1,
    )
    order_writer = ToolSpec(
        name="order_writer",
        required_permissions=("write:orders",),
        connector=_connector,
        tier=Tier.T2,
    )
    registry.register(catalog_search)
    registry.register(order_writer)
    definition = _definition(
        tools=("catalog_search", "order_writer"),
        permissions=("read:catalog", "write:orders"),
        untrusted_input=True,
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("read:catalog", "write:orders"),
    )

    assert result.granted == (catalog_search, order_writer)
    assert result.denied == ()


def test_real_sales_agent_definition_never_grants_hypothetical_t3_tool() -> None:
    """Injection test named by issue #109: resolve `sales-agent` for real
    (untrusted_input=True per #108), then simulate a future manifest change
    that adds a T3 tool with its matching permission — resolve_tool_surface
    must still exclude it, independent of anything loader.resolve() checks.
    """
    import dataclasses

    from agents_system.harness.injector import resolve_tool_surface
    from agents_system.harness.loader import RootConfig, resolve

    real_definition = resolve("sales-agent", roots=RootConfig())
    assert real_definition.untrusted_input is True

    registry = ToolRegistry()
    for name in real_definition.tools:
        registry.register(
            ToolSpec(
                name=name,
                required_permissions=(),
                connector=_connector,
                tier=Tier.T1,
            )
        )
    hypothetical_t3 = ToolSpec(
        name="hypothetical_read_file",
        required_permissions=("read:files",),
        connector=_connector,
        tier=Tier.T3,
    )
    registry.register(hypothetical_t3)

    hypothetical_definition = dataclasses.replace(
        real_definition,
        tools=real_definition.tools + ("hypothetical_read_file",),
        permissions=real_definition.permissions + ("read:files",),
    )

    result = resolve_tool_surface(
        hypothetical_definition,
        registry,
        granted_permissions=set(hypothetical_definition.permissions),
    )

    granted_names = {spec.name for spec in result.granted}
    assert "hypothetical_read_file" not in granted_names


# ---------------------------------------------------------------------------
# R3 — grant coverage of permission descendants
# ---------------------------------------------------------------------------


def test_ancestor_grant_covers_same_tier_descendant() -> None:
    """A grant of the ancestor covers a required, same-tier registered
    descendant — `issubclass` holds and tiers are equal (0<=0)."""
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    spec = ToolSpec(
        name="descendant_tool",
        required_permissions=("read:injector_r3_same_tier_descendant",),
        connector=_connector,
        tier=Tier.T1,
    )
    registry.register(spec)
    definition = _definition(
        tools=("descendant_tool",),
        permissions=("read:injector_r3_ancestor",),
    )

    result = resolve_tool_surface(
        definition, registry, granted_permissions=("read:injector_r3_ancestor",)
    )

    assert result.granted == (spec,)
    assert result.denied == ()


def test_exact_class_grant_covers_itself() -> None:
    """A grant naming the exact required class always covers it (`D is P`)."""
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    spec = ToolSpec(
        name="exact_tool",
        required_permissions=("read:injector_r3_same_tier_descendant",),
        connector=_connector,
        tier=Tier.T1,
    )
    registry.register(spec)
    definition = _definition(
        tools=("exact_tool",),
        permissions=("read:injector_r3_same_tier_descendant",),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        granted_permissions=("read:injector_r3_same_tier_descendant",),
    )

    assert result.granted == (spec,)
    assert result.denied == ()


def test_ancestor_grant_does_not_cover_escalated_descendant() -> None:
    """`SensitiveExport`-shaped case: an ancestor grant MUST NOT cover a
    descendant that escalated its own tier (`2<=0` is false) — the
    escalated class needs its own explicit grant."""
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    spec = ToolSpec(
        name="escalated_tool",
        required_permissions=("read:injector_r3_escalated_descendant",),
        connector=_connector,
        tier=Tier.T2,
    )
    registry.register(spec)
    definition = _definition(
        tools=("escalated_tool",),
        permissions=(
            "read:injector_r3_ancestor",
            "read:injector_r3_escalated_descendant",
        ),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        # Only the ancestor is granted -- the escalated descendant is not.
        granted_permissions=("read:injector_r3_ancestor",),
    )

    assert result.granted == ()
    assert result.denied == (
        (
            "escalated_tool",
            "missing permissions: read:injector_r3_escalated_descendant",
        ),
    )


def test_unrelated_class_grant_never_covers_by_tier_alone() -> None:
    """A grant of `Exec` (T3) MUST NOT cover a required `Write` (T2) —
    `Write` is not a subclass of `Exec`, regardless of tier ordering."""
    from agents_system.harness.injector import resolve_tool_surface

    registry = ToolRegistry()
    spec = ToolSpec(
        name="write_tool",
        required_permissions=("write:orders",),
        connector=_connector,
        tier=Tier.T2,
    )
    registry.register(spec)
    definition = _definition(
        tools=("write_tool",),
        permissions=("exec:command", "write:orders"),
    )

    result = resolve_tool_surface(
        definition,
        registry,
        # Only the unrelated, higher-tier Exec permission is granted.
        granted_permissions=("exec:command",),
    )

    assert result.granted == ()
    assert result.denied == (("write_tool", "missing permissions: write:orders"),)
