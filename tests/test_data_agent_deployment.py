"""The BADIE data-agent deployment must actually resolve (D-023).

These manifests are data, not code, so nothing type-checks them and nothing
imports them — a typo in a tool name or a permission that widens the parent
surface fails at runtime, inside `resolve_tool_surface`, when the app boots.

The role has already been unbuildable once for exactly this reason:
`knowledge_retrieval` was declared through v1.0 with no connector behind it,
so `resolve_tool_surface` raised `InjectionError: Unknown tool` and the whole
role could not be built. Not partially — at all.
"""
from __future__ import annotations

from agentsys.harness.loader import load_generic, resolve


def test_badie_data_agent_deployment_resolves() -> None:
    definition = resolve("data-agent", client="badie")

    assert definition.tools, "resolved definition has no tools"
    assert definition.permissions, "resolved definition has no permissions"


def test_deployment_tools_are_a_subset_of_the_platform_role() -> None:
    """A deployment may only NARROW the platform surface, never widen it.

    This is the invariant the whole two-layer model rests on: injection can
    only subtract. A deployment that names a tool the platform role does not
    grant has escaped the platform's own ceiling.
    """
    platform = load_generic("data-agent")
    deployment = resolve("data-agent", client="badie")

    extra = set(deployment.tools) - set(platform.tools)
    assert not extra, f"deployment widens the platform tool surface with {extra}"


def test_deployment_permissions_are_a_subset_of_the_platform_role() -> None:
    platform = load_generic("data-agent")
    deployment = resolve("data-agent", client="badie")

    extra = set(deployment.permissions) - set(platform.permissions)
    assert not extra, f"deployment widens the platform permissions with {extra}"


def test_every_deployment_tool_has_the_permission_it_needs_declared() -> None:
    """`run_report` requires `read:reports`, and the role must actually grant it.

    A tool the injector cannot satisfy does not raise — it lands in
    `InjectionResult.denied` and the agent simply never sees it, which reads
    as "the model chose not to use the report tool" rather than as a
    misconfiguration.
    """
    definition = resolve("data-agent", client="badie")

    assert "run_report" in definition.tools
    assert "read:reports" in definition.permissions


# ---------------------------------------------------------------------------
# The platform must be able to equip everything its own manifest names
#
# This role has been unbuildable on `main` for a while: the manifest lists
# `knowledge_retrieval` and no such connector exists, so resolve_tool_surface
# raises InjectionError. Not partially usable — entirely unbuildable. Slice 5
# is where the manifest becomes honest again, so this is where the invariant
# can finally be asserted instead of described in a comment.
# ---------------------------------------------------------------------------


def test_every_data_agent_manifest_tool_can_be_equipped() -> None:
    """The invariant, stated directly against the manifest.

    Reproduces what a consumer does: resolve the generic role, then ask the
    platform's own registry to equip it. Any tool the manifest names and the
    registry lacks raises InjectionError here.
    """
    from agentsys.connectors.stubs import build_badie_registry
    from agentsys.harness import loader
    from agentsys.harness.injector import resolve_tool_surface

    definition = loader.resolve("data-agent", client=None)
    result = resolve_tool_surface(
        definition, build_badie_registry(), granted_permissions=definition.permissions
    )

    assert result.denied == ()
    assert {spec.name for spec in result.granted} == set(definition.tools)
