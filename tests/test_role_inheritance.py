"""Role-to-role inheritance: `extends:` resolved into a chain.

Two inheritance relations exist and they run in opposite directions.

**role -> role is additive.** A child adds capability to its parent. Both
sides are authored by the platform, so no trust boundary is crossed and
widening is the point: `sales-agent extends agent` must end up with agent's
tools plus its own.

**deployment -> role stays subtractive.** That relation crosses a trust
boundary — a consumer must never grant itself more than its platform role
allows — and `_merge_validated` already enforces it. Because the chain is
resolved inside `load_generic`, that existing validation now runs against the
whole resolved chain for free. `test_role_resolution_pinned.py` guards it.

Two things stay subtractive even in the additive direction: `execution_limits`
and `autonomy`. Adding a tool is a capability decision the platform author
owns. Raising a timeout or loosening supervision is a safety decision, and a
child that could do it silently would make the root's limits decorative.
"""
from __future__ import annotations

import pathlib

import pytest

from agentsys.harness.loader import DefinitionError, RootConfig, resolve

_FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "agents" / "hierarchy"


def _roots() -> RootConfig:
    return RootConfig(platform_root=_FIXTURES)


# --- The chain resolves ----------------------------------------------------


def test_child_inherits_its_parents_tools_and_permissions() -> None:
    definition = resolve("fx-mid", roots=_roots())

    assert set(definition.tools) == {"tool_mid"}
    assert set(definition.permissions) == {"read:base", "read:mid"}


def test_grandchild_inherits_through_two_levels() -> None:
    """The property that makes this a hierarchy rather than one override.

    `fx-leaf` extends `fx-mid`, which extends `fx-base`. The leaf declares
    neither `tool_mid` nor `read:base`, and must end up with both.
    """
    definition = resolve("fx-leaf", roots=_roots())

    assert set(definition.tools) == {"tool_mid", "tool_leaf"}
    assert set(definition.permissions) == {"read:base", "read:mid", "read:leaf"}


def test_child_context_keys_override_the_parents() -> None:
    """A child refines context rather than replacing it wholesale.

    `fx-base` sets `user_identity: false`; `fx-mid` sets it true and says
    nothing about `session`, which must survive from the parent.
    """
    definition = resolve("fx-mid", roots=_roots())

    assert definition.context["user_identity"] is True
    assert definition.context["session"] is True


def test_extends_accepts_both_the_path_form_and_a_bare_name() -> None:
    """Existing manifests write `extends: platform/roles/<name>`.

    That form is already on disk in every deployment manifest, so it has to
    keep meaning what it looks like it means. A bare role name works too.
    """
    # fx-mid uses the path form, fx-leaf uses the bare name.
    assert set(resolve("fx-mid", roots=_roots()).tools) == {"tool_mid"}
    assert "tool_mid" in set(resolve("fx-leaf", roots=_roots()).tools)


# --- Broken chains fail loudly ---------------------------------------------


def test_a_cycle_raises_and_names_the_cycle() -> None:
    """It must be reported AS a cycle, not merely caught by the depth cap.

    An earlier version of this test asserted only that both role names
    appeared in the message. That passed with cycle detection deleted,
    because the walk then ran until `_MAX_ROLE_CHAIN_DEPTH` and the depth
    error also lists every role it visited. The test could not tell the two
    mechanisms apart, which is the whole thing it exists to check.
    """
    with pytest.raises(DefinitionError) as excinfo:
        resolve("fx-cycle-a", roots=_roots())

    message = str(excinfo.value)
    assert "cycle" in message.lower()
    assert "deeper than" not in message
    assert "fx-cycle-a" in message
    assert "fx-cycle-b" in message


def test_a_chain_deeper_than_the_cap_raises_a_depth_error(
    tmp_path: pathlib.Path,
) -> None:
    """The cap is a separate guard, and reports itself separately.

    Built here rather than committed as fixtures because a legitimate chain
    this deep should never exist on disk. It also lets the cycle test above
    rely on the two error messages being distinguishable.
    """
    from agentsys.harness import loader

    depth = loader._MAX_ROLE_CHAIN_DEPTH + 2
    roles_dir = tmp_path / "roles"
    for i in range(depth):
        folder = roles_dir / f"deep-{i}"
        folder.mkdir(parents=True)
        parent = f"\nextends: deep-{i + 1}" if i < depth - 1 else ""
        (folder / "role.md").write_text(
            f'---\nname: deep-{i}\nversion: "1.0"\n---\n\nbody\n', encoding="utf-8"
        )
        (folder / "manifest.md").write_text(
            f'---\nrole: deep-{i}{parent}\ntools: []\nskills: []\n'
            f"context: {{}}\npermissions: []\n---\n\nmanifest\n",
            encoding="utf-8",
        )
        (folder / "policy.md").write_text(
            f'---\nrole: deep-{i}\nautonomy: supervised\n'
            f"execution_limits: null\n---\n\npolicy\n",
            encoding="utf-8",
        )

    with pytest.raises(DefinitionError) as excinfo:
        resolve("deep-0", roots=RootConfig(platform_root=tmp_path))

    message = str(excinfo.value)
    assert "deeper than" in message
    assert "cycle" not in message.lower()


def test_extending_a_role_that_does_not_exist_raises() -> None:
    with pytest.raises(DefinitionError) as excinfo:
        resolve("fx-orphan", roots=_roots())

    assert "fx-does-not-exist" in str(excinfo.value)


# --- Abstract roles cannot be instantiated ---------------------------------


def test_an_abstract_role_cannot_be_resolved_directly() -> None:
    with pytest.raises(DefinitionError) as excinfo:
        resolve("fx-base", roots=_roots())

    assert "fx-base" in str(excinfo.value)
    assert "abstract" in str(excinfo.value).lower()


def test_an_abstract_role_may_still_be_a_parent() -> None:
    """Being un-instantiable is the point of being abstract, not a defect.

    If `abstract: true` also blocked inheritance the directive would be
    useless — there would be nothing an abstract role could do.
    """
    definition = resolve("fx-mid", roots=_roots())

    assert "read:base" in definition.permissions


# --- Safety invariants stay subtractive ------------------------------------


def test_a_child_may_not_loosen_its_parents_execution_limits() -> None:
    """Additive for capability, never for safety.

    `fx-strict` caps the turn at 10s and 3 tool calls. A child asking for 999
    and 99 is asking to escape a ceiling its parent set, and the platform's
    limits would mean nothing if that were allowed silently.
    """
    with pytest.raises(DefinitionError) as excinfo:
        resolve("fx-loosens-limits", roots=_roots())

    assert "total_execution_timeout_s" in str(excinfo.value)


def test_a_child_may_not_loosen_its_parents_autonomy() -> None:
    """`supervised` -> `full` is an escalation of trust, not an addition."""
    with pytest.raises(DefinitionError) as excinfo:
        resolve("fx-loosens-autonomy", roots=_roots())

    assert "autonomy" in str(excinfo.value).lower()


# --- The declaration is no longer inert ------------------------------------


def test_the_loader_actually_reads_extends() -> None:
    """The regression this whole change exists to fix.

    Before it, `extends:` appeared in every deployment manifest and was read
    by nobody — `rg -n 'extends' src/` returned one unrelated docstring. The
    parent was deduced from the directory name, so the declaration read as
    authoritative to every human and every agent while doing nothing.

    This asserts on source rather than behaviour on purpose: behaviour alone
    could be satisfied by a directory convention that happens to agree.
    """
    from agentsys.harness import loader

    source = pathlib.Path(loader.__file__).read_text(encoding="utf-8")

    assert "extends" in source
