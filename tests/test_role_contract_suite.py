"""ADR-002 D.17 -- the formalized inherited role contract test suite.

This is the named, documented convention the issue asks for: one place that
walks `discover_concrete_platform_roles()` (`platform_role_contract.py`) and
checks a battery of contract invariants against every role it finds, so a
contract asserted once here is checked against every CURRENT and FUTURE
descendant automatically -- no per-role test to remember to add. It builds
directly on two existing templates: `test_role_resolution_pinned.py`'s
`test_no_role_outside_the_operator_branch_can_reach_the_host` (a security
property checked tree-wide) and `platform_role_contract.py`'s own
`discover_concrete_platform_roles` (the tree walk itself).

Every check applied below is a small, reusable function in
`platform_role_contract.py` (its "ADR-002 D.17" section), operating on an
already-resolved `AgentDefinition` (or a computed tool-name set) rather than
on disk. That is what makes each one independently provable per Strict TDD:
a synthetic, DELIBERATELY BROKEN value is built with `dataclasses.replace`
on a real resolution and handed straight to the check -- the same
one-layer-up-from-disk pattern `test_untrusted_input_invariant.py`'s
`_raw()` helper already uses for `RawDefinition`.

**What this file does NOT re-implement** -- already tree-walked elsewhere,
reused here rather than copied:

- Resolves without error: implied by every check below (each starts by
  resolving a real role); further proven throughout
  `test_harness_loader.py` / `test_harness_loader_edge.py` (cycles, missing
  files, bad YAML all raise `DefinitionError`).
- Host access confined to the operator branch:
  `test_role_resolution_pinned.py::
  test_no_role_outside_the_operator_branch_can_reach_the_host` -- the exact
  template this issue formalizes.
- `untrusted_input` explicitly declared, and its real-tree marking:
  `test_untrusted_input_invariant.py::
  test_every_concrete_platform_role_explicitly_declares_untrusted_input` /
  `test_platform_role_untrusted_input_marking`.
- Every manifest tool is registrable AND equippable (the registry holds it,
  the role's OWN permissions grant it, nothing unexpected is denied):
  `test_platform_tools_integration.py::test_every_platform_role_boots_end_to_end`.
- The base contract's six clauses and design-notes stripping, at the
  loader-mechanics level (near-miss headings, fenced/indented code blocks,
  the factory-composed prompt once skills are in the mix):
  `test_prompt_contract.py`.

New here, none of which had a tree-wide, auto-discovering regression net
before this change: design-notes leakage and the base contract's "present
exactly once, last block" shape checked against EVERY concrete role (not
four hardcoded ones); permissions and tools surviving the whole `extends:`
chain; and a declared tool never being equippable without the permission
its registry spec requires.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

import pytest
from platform_role_contract import (
    check_base_contract_present_once_and_last,
    check_escalation_conditions_have_descriptions,
    check_no_design_notes_leak,
    check_permissions_and_tools_survive_inheritance,
    check_ungranted_tools_are_not_injected,
    check_untrusted_input_exec_exclusion,
    discover_concrete_platform_roles,
    is_abstract,
    role_chain,
)

from agents_system.harness.injector import resolve_tool_surface
from agents_system.harness.loader import resolve


def _registry() -> Any:
    from conftest import build_test_registry

    return build_test_registry()


# ---------------------------------------------------------------------------
# Tree-wide: every concrete role, walked automatically via
# `discover_concrete_platform_roles()`. A role added to `platform/roles/`
# tomorrow is covered here without editing this file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", discover_concrete_platform_roles())
def test_role_resolves_without_error(role: str) -> None:
    resolve(role)


@pytest.mark.parametrize("role", discover_concrete_platform_roles())
def test_no_design_notes_leak(role: str) -> None:
    check_no_design_notes_leak(resolve(role))


@pytest.mark.parametrize("role", discover_concrete_platform_roles())
def test_base_contract_present_once_and_last(role: str) -> None:
    check_base_contract_present_once_and_last(resolve(role))


@pytest.mark.parametrize("role", discover_concrete_platform_roles())
def test_untrusted_input_exec_exclusion(role: str) -> None:
    check_untrusted_input_exec_exclusion(resolve(role))


@pytest.mark.parametrize("role", discover_concrete_platform_roles())
def test_escalation_conditions_have_descriptions(role: str) -> None:
    check_escalation_conditions_have_descriptions(resolve(role))


@pytest.mark.parametrize("role", discover_concrete_platform_roles())
def test_permissions_and_tools_survive_inheritance(role: str) -> None:
    leaf = resolve(role)
    for ancestor in role_chain(role)[1:]:
        if is_abstract(ancestor):
            # An abstract ancestor (e.g. `base`) cannot be resolve()d on its
            # own by design -- nothing further to independently check here.
            continue
        check_permissions_and_tools_survive_inheritance(leaf, resolve(ancestor))


@pytest.mark.parametrize("role", discover_concrete_platform_roles())
def test_tools_requiring_a_permission_are_not_injected_without_it(role: str) -> None:
    definition = resolve(role)
    registry = _registry()

    surface = resolve_tool_surface(definition, registry, granted_permissions=())
    granted_names = frozenset(spec.name for spec in surface.granted)

    check_ungranted_tools_are_not_injected(definition, registry, granted_names)


# ---------------------------------------------------------------------------
# Mutation-style proof (Strict TDD): each check above can actually fail, on
# a deliberately broken synthetic value built from a real resolution.
# ---------------------------------------------------------------------------


def test_design_notes_check_catches_a_leaked_marker() -> None:
    broken = dataclasses.replace(
        resolve("agent"),
        system_prompt=(
            "Answer customer questions.\n\n"
            "## design notes\n\n"
            "Internal rationale nobody using the product should ever see."
        ),
    )
    with pytest.raises(AssertionError):
        check_no_design_notes_leak(broken)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda prompt: "A role prompt with no base contract at all.",
        lambda prompt: prompt + "\n\nOne more instruction appended after it.",
    ],
    ids=["contract-missing", "contract-not-last"],
)
def test_base_contract_check_catches_a_broken_prompt(
    mutate: Callable[[str], str],
) -> None:
    real = resolve("agent")
    broken = dataclasses.replace(real, system_prompt=mutate(real.system_prompt))

    with pytest.raises(AssertionError):
        check_base_contract_present_once_and_last(broken)


def test_untrusted_input_exec_exclusion_check_catches_the_lethal_combo() -> None:
    broken = dataclasses.replace(
        resolve("agent"), untrusted_input=True, permissions=("exec:shell",)
    )
    with pytest.raises(AssertionError):
        check_untrusted_input_exec_exclusion(broken)


def test_escalation_description_check_catches_an_undescribed_condition() -> None:
    real = resolve("agent")
    broken = dataclasses.replace(
        real,
        escalation_rules={
            **real.escalation_rules,
            "conditions": [*real.escalation_rules["conditions"], "made_up_condition"],
        },
    )
    with pytest.raises(AssertionError):
        check_escalation_conditions_have_descriptions(broken)


def test_inheritance_monotonic_check_catches_a_dropped_permission() -> None:
    # `sales-agent` inherits `send:escalation` from `agent` (pinned in
    # `test_role_resolution_pinned.py::PINNED_PLATFORM`) -- drop it from a
    # synthetic copy to prove the check notices a lost inherited grant.
    parent = resolve("agent")
    child = resolve("sales-agent")
    broken_child = dataclasses.replace(
        child,
        permissions=tuple(p for p in child.permissions if p != "send:escalation"),
    )

    with pytest.raises(AssertionError):
        check_permissions_and_tools_survive_inheritance(broken_child, parent)


def test_ungranted_tool_check_catches_a_wrongly_granted_tool() -> None:
    # `resolve_tool_surface`'s permission gate is already correct and
    # unconditionally enforced, so no legitimate role/registry pair can
    # violate this today -- proving the check has teeth means handing it a
    # deliberately wrong "everything was granted" result directly, as if
    # that gate had broken.
    definition = resolve("sales-agent")
    registry = _registry()
    wrongly_granted_everything = frozenset(definition.tools)

    with pytest.raises(AssertionError):
        check_ungranted_tools_are_not_injected(
            definition, registry, wrongly_granted_everything
        )


def test_platform_role_contract_module_uses_predefined_terminology() -> None:
    """PR5 (ADR-004): `platform_role_contract.py`'s own doc language for the
    eight packaged roles says "predefined role", never "generic role"
    (agent-definition-locator spec's Terminology section) -- "generic" stays
    reserved for the abstract base->agent tree, which this module never
    names in its own docs/comments anyway.
    """
    import inspect
    import pathlib

    from platform_role_contract import discover_platform_roles

    source = pathlib.Path(inspect.getfile(discover_platform_roles)).read_text()
    assert "generic role" not in source.lower()
    assert "predefined role" in (discover_platform_roles.__doc__ or "").lower()
