"""ADR-002 C.11 — `untrusted_input` flag and `exec:*` mutual-exclusion invariant.

Strict TDD: written BEFORE the implementation, intentionally red until:
- `RawDefinition`/`AgentDefinition` carry `untrusted_input`
- the mutual-exclusion invariant (`untrusted_input=true` + any `exec:*`
  permission -> `DefinitionError`) is enforced on BOTH `resolve()` return
  paths — the `merge(generic, override)` branch and the no-override branch,
  which previously skipped every merge-time validation
- a deployment override cannot flip a resolved `untrusted_input=true` back to
  `false` (monotonic, mirroring `_validate_autonomy`'s rank-comparison
  pattern)
- every existing platform role is marked per the ADR's initial marking

Fixtures are built on disk under `tmp_path` (mirroring the `_chain()` helper
in `test_role_inheritance.py`) rather than committed as static fixture
files, since a synthetic "rogue" role should never live permanently under
`platform/roles/`.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from agents_system.harness.loader import (
    DefinitionError,
    RawDefinition,
    RootConfig,
    merge,
    resolve,
)
from platform_role_contract import discover_concrete_platform_roles


# ---------------------------------------------------------------------------
# On-disk fixture helpers
# ---------------------------------------------------------------------------
def _write_role(
    platform_root: pathlib.Path,
    name: str,
    *,
    extends: str | None = None,
    permissions: list[str] | None = None,
    untrusted_input: bool | None = None,
    autonomy: str = "supervised",
) -> None:
    """Write a minimal platform role folder, optionally with `extends:`."""
    folder = platform_root / "roles" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "role.md").write_text(
        f"---\nname: {name}\n---\n\nbody\n", encoding="utf-8"
    )
    perms = permissions or []
    perms_yaml = "\n".join(f"  - {p}" for p in perms) or "  []"
    ext_line = f"extends: {extends}\n" if extends else ""
    (folder / "manifest.md").write_text(
        f"---\nrole: {name}\n{ext_line}tools: []\nskills: []\ncontext: {{}}\n"
        f"permissions:\n{perms_yaml}\n---\n\nm\n",
        encoding="utf-8",
    )
    ui_line = (
        f"untrusted_input: {'true' if untrusted_input else 'false'}\n"
        if untrusted_input is not None
        else ""
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {name}\nautonomy: {autonomy}\n{ui_line}"
        "execution_limits: null\n---\n\np\n",
        encoding="utf-8",
    )


def _write_override(
    deployments_root: pathlib.Path,
    client: str,
    role_type: str,
    *,
    permissions: str = "inherit",
    untrusted_input: bool | None = None,
) -> None:
    """Write a minimal deployment override folder for an existing role."""
    folder = deployments_root / client / role_type
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "role.md").write_text(
        f"---\nname: {role_type}\n---\n\nbody\n", encoding="utf-8"
    )
    (folder / "manifest.md").write_text(
        f"---\nrole: {role_type}\ndeployment: {client}\ntools: []\nskills: []\n"
        f"context: {{}}\npermissions: {permissions}\n---\n\nm\n",
        encoding="utf-8",
    )
    ui_line = (
        f"untrusted_input: {'true' if untrusted_input else 'false'}\n"
        if untrusted_input is not None
        else ""
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {role_type}\ndeployment: {client}\n{ui_line}"
        "execution_limits: inherit\n---\n\np\n",
        encoding="utf-8",
    )


def _write_role_with_raw_policy_line(
    platform_root: pathlib.Path, name: str, *, policy_line: str
) -> None:
    """Like `_write_role`, but injects an arbitrary raw YAML line into
    policy.md's frontmatter — for values `_write_role`'s bool-only
    `untrusted_input` parameter cannot express (a quoted string, an int)."""
    folder = platform_root / "roles" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "role.md").write_text(
        f"---\nname: {name}\n---\n\nbody\n", encoding="utf-8"
    )
    (folder / "manifest.md").write_text(
        f"---\nrole: {name}\ntools: []\nskills: []\ncontext: {{}}\n"
        "permissions:\n  []\n---\n\nm\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {name}\nautonomy: supervised\n{policy_line}\n"
        "execution_limits: null\n---\n\np\n",
        encoding="utf-8",
    )


def _raw(**overrides: Any) -> RawDefinition:
    """Build a RawDefinition with sensible, already-resolved defaults."""
    base: dict[str, Any] = dict(
        role_name="role",
        version="1.0",
        deployment=None,
        system_prompt="",
        tools=[],
        skills=[],
        context={},
        permissions=[],
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
        # A generic role coming out of `load_generic()` always has a
        # concrete bool here (defaulted to False if never declared) — never
        # `None`. `None` is only meaningful on an *override*, meaning "not
        # declared, inherit".
        untrusted_input=False,
    )
    base.update(overrides)
    return RawDefinition(**base)


# ---------------------------------------------------------------------------
# Mutual exclusion — untrusted_input=true + any exec:* permission
# ---------------------------------------------------------------------------
def test_no_override_branch_rejects_untrusted_input_with_exec_permission(
    tmp_path: pathlib.Path,
) -> None:
    """The branch that used to skip every merge-time validation.

    `resolve(role_type)` with no `client` returns straight from `generic`
    (`loader.py`'s no-override branch of `resolve()`), bypassing `merge()`
    and everything it validates. The invariant must be checked here too.
    """
    _write_role(
        tmp_path, "rogue-agent", permissions=["exec:shell"], untrusted_input=True
    )

    with pytest.raises(DefinitionError, match="untrusted_input"):
        resolve("rogue-agent", roots=RootConfig(platform_root=tmp_path))


def test_merge_branch_rejects_untrusted_input_with_exec_permission(
    tmp_path: pathlib.Path,
) -> None:
    """The invariant must hold when the conflict is only complete after merge.

    The platform role alone holds `exec:shell` but says nothing about
    `untrusted_input`; the deployment override is the one that declares
    `untrusted_input: true`. Neither file alone violates the invariant — only
    the resolved, merged definition does.
    """
    _write_role(tmp_path, "rogue-agent", permissions=["exec:shell"])
    deployments = tmp_path / "deployments"
    _write_override(deployments, "acme", "rogue-agent", untrusted_input=True)

    roots = RootConfig(platform_root=tmp_path, deployments_root=deployments)
    with pytest.raises(DefinitionError, match="untrusted_input"):
        resolve("rogue-agent", client="acme", roots=roots)


# ---------------------------------------------------------------------------
# Monotonic, once true — a child/override cannot flip true back to false
# ---------------------------------------------------------------------------
def test_deployment_override_cannot_reset_untrusted_input_to_false(
    tmp_path: pathlib.Path,
) -> None:
    _write_role(tmp_path, "trusted-flip", untrusted_input=True)
    deployments = tmp_path / "deployments"
    _write_override(deployments, "acme", "trusted-flip", untrusted_input=False)

    roots = RootConfig(platform_root=tmp_path, deployments_root=deployments)
    with pytest.raises(DefinitionError, match="untrusted_input"):
        resolve("trusted-flip", client="acme", roots=roots)


def test_the_two_directions_that_must_stay_legal(tmp_path: pathlib.Path) -> None:
    """false -> true (widening) and "declares nothing" (inherits) are not violations."""
    deployments = tmp_path / "deployments"
    _write_role(tmp_path, "widened", untrusted_input=False)
    _write_override(deployments, "acme", "widened", untrusted_input=True)
    _write_role(tmp_path, "silent", untrusted_input=True)
    _write_override(deployments, "acme", "silent", untrusted_input=None)

    roots = RootConfig(platform_root=tmp_path, deployments_root=deployments)
    assert resolve("widened", client="acme", roots=roots).untrusted_input is True
    assert resolve("silent", client="acme", roots=roots).untrusted_input is True


# ---------------------------------------------------------------------------
# Monotonic, once true — the `extends:` chain fold, not only deployments.
#
# `_fold_parent_into_child` (role-to-role composition) previously resolved
# `untrusted_input` with a plain "child wins if declared" rule and no
# validation at all — so a platform role `extends: sales-agent`
# (untrusted_input=true) could declare `untrusted_input: false` and then add
# `exec:*`: the deployment-override check never sees a conflict, because by
# the time `resolve()` reaches it the chain has already silently resolved to
# `false`. That is the exact lethal-trifecta gap C.11 exists to close.
# ---------------------------------------------------------------------------
def test_chain_child_cannot_reset_untrusted_input_to_false(
    tmp_path: pathlib.Path,
) -> None:
    _write_role(tmp_path, "trusted-parent", untrusted_input=True)
    _write_role(tmp_path, "flips-it", extends="trusted-parent", untrusted_input=False)

    with pytest.raises(DefinitionError, match="untrusted_input"):
        resolve("flips-it", roots=RootConfig(platform_root=tmp_path))


def test_chain_child_flipping_false_while_adding_exec_is_still_rejected(
    tmp_path: pathlib.Path,
) -> None:
    """The exact attack: flip the flag, then add exec:* on the same role.

    Must be rejected by the monotonicity check itself — the flip is already
    illegal, so the mutual-exclusion invariant never even needs to run for
    the trifecta to be blocked.
    """
    _write_role(tmp_path, "trusted-parent", untrusted_input=True)
    _write_role(
        tmp_path,
        "flips-and-execs",
        extends="trusted-parent",
        untrusted_input=False,
        permissions=["exec:shell"],
    )

    with pytest.raises(DefinitionError, match="untrusted_input"):
        resolve("flips-and-execs", roots=RootConfig(platform_root=tmp_path))


def test_chain_three_levels_deep_rejects_a_false_two_levels_down(
    tmp_path: pathlib.Path,
) -> None:
    """A violation must be caught at whichever fold step introduces it.

    `_resolve_role_chain` folds root-first, one level at a time. The middle
    role here declares nothing (inherits `true`); only the leaf, two levels
    below the `true` root, declares `false`.
    """
    _write_role(tmp_path, "root", untrusted_input=True)
    _write_role(tmp_path, "middle", extends="root")
    _write_role(tmp_path, "leaf", extends="middle", untrusted_input=False)

    with pytest.raises(DefinitionError, match="untrusted_input"):
        resolve("leaf", roots=RootConfig(platform_root=tmp_path))


# ---------------------------------------------------------------------------
# The root default (False) must never be what makes a REAL role's resolved
# value. `load_generic()` still defaults an undeclared chain to `False` (the
# same "apply the floor once, after folding" pattern `autonomy` already
# uses) — but for THIS flag, unlike `autonomy`, silently defaulting to
# `false` is fail-OPEN: it disarms the exec:* invariant for a role that
# actually needs `true` but whose author forgot to declare it. That default
# is only acceptable because every concrete platform role's chain passes
# through `agent` (or `base`), both of which now declare the field
# explicitly (see platform/roles/agent/policy.md,
# platform/roles/base/policy.md) — so the implicit default is dead code for
# the real, shipped role surface. This test makes that a checked guarantee,
# not an incidental fact: it fails the moment a new concrete role lands
# without extending agent/base AND without declaring its own value.
#
# A blanket "fail at load for ANY concrete role with no declaration" was
# considered and rejected: dozens of pre-existing, unrelated test fixtures
# across the suite (test_role_inheritance.py's fx-* roles,
# test_harness_loader.py's simple-role, etc.) declare no untrusted_input at
# all and would all have to be touched, which is out of proportion to this
# issue's scope.
# ---------------------------------------------------------------------------
def test_every_concrete_platform_role_explicitly_declares_untrusted_input() -> None:
    from agents_system.harness.loader import _extends_target, _read_md

    roots = RootConfig()
    for role in discover_concrete_platform_roles():
        current: str | None = role
        seen: set[str] = set()
        declared = False
        while current is not None and current not in seen:
            seen.add(current)
            role_dir = roots.platform_root / "roles" / current
            policy_fm, _ = _read_md(role_dir / "policy.md")
            if "untrusted_input" in policy_fm:
                declared = True
                break
            manifest_fm, _ = _read_md(role_dir / "manifest.md")
            parent_raw = manifest_fm.get("extends")
            current = _extends_target(parent_raw) if parent_raw else None
        assert declared, (
            f"'{role}' never explicitly declares untrusted_input anywhere "
            "in its extends chain — it would silently resolve through the "
            "unsafe implicit False default"
        )


# ---------------------------------------------------------------------------
# exec:* prefix match must be case-insensitive and whitespace-tolerant
# (review follow-up: `EXEC:shell` with untrusted_input=true resolved
# cleanly, silently disarming the lethal-trifecta guard on a typo/case
# variant that is still, functionally, an exec:* permission).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("permission", ["EXEC:shell", "Exec:Shell", "  exec:shell"])
def test_exec_prefix_check_is_case_and_whitespace_insensitive(permission: str) -> None:
    generic = _raw(
        role_name="case-role", permissions=[permission], untrusted_input=False
    )
    override = _raw(
        role_name="case-role",
        deployment="case-deployment",
        permissions="inherit",
        untrusted_input=True,
    )

    with pytest.raises(DefinitionError, match="untrusted_input"):
        merge(generic, override)


# ---------------------------------------------------------------------------
# `untrusted_input` must be a real YAML bool (review follow-up: a quoted
# `"false"` string is truthy in Python, and an int `0`/`1` would silently
# coerce — either is a typo that SILENTLY DISARMS the invariant for
# whatever role declares it).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("policy_line", "type_name"),
    [
        ('untrusted_input: "false"', "str"),
        ("untrusted_input: 1", "int"),
        ("untrusted_input: null", "NoneType"),
    ],
)
def test_untrusted_input_rejects_non_bool_yaml_values(
    tmp_path: pathlib.Path, policy_line: str, type_name: str
) -> None:
    _write_role_with_raw_policy_line(tmp_path, "bad-type-role", policy_line=policy_line)

    with pytest.raises(DefinitionError, match="untrusted_input"):
        resolve("bad-type-role", roots=RootConfig(platform_root=tmp_path))


# ---------------------------------------------------------------------------
# Truth table (ADR-002 C.11)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("untrusted_input", "has_exec_permission", "should_raise"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, True),
    ],
)
def test_truth_table(
    untrusted_input: bool, has_exec_permission: bool, should_raise: bool
) -> None:
    permissions = ["exec:shell"] if has_exec_permission else []
    generic = _raw(role_name="tt-role", permissions=permissions, untrusted_input=False)
    override = _raw(
        role_name="tt-role",
        deployment="tt-deployment",
        permissions="inherit",
        untrusted_input=untrusted_input,
    )

    if should_raise:
        with pytest.raises(DefinitionError, match="untrusted_input"):
            merge(generic, override)
    else:
        result = merge(generic, override)
        assert result.untrusted_input is untrusted_input


# ---------------------------------------------------------------------------
# Regression — every existing platform role's initial marking
# ---------------------------------------------------------------------------
_EXPECTED_UNTRUSTED_INPUT = {
    "sales-agent": True,
    "support-agent": True,
    "data-agent": False,
    "summary-agent": False,
    "accountant-agent": False,
    "orchestrator": False,
    "operator-agent": False,
    "developer-agent": False,
    # Not named by the issue, but concrete (resolvable) and therefore in
    # scope: the shared internal base every conversational role but
    # sales/support descends from.
    "agent": False,
}


@pytest.mark.parametrize("role_type", sorted(_EXPECTED_UNTRUSTED_INPUT))
def test_platform_role_untrusted_input_marking(role_type: str) -> None:
    definition = resolve(role_type)

    assert definition.untrusted_input is _EXPECTED_UNTRUSTED_INPUT[role_type], (
        f"'{role_type}' resolved untrusted_input="
        f"{definition.untrusted_input}, expected "
        f"{_EXPECTED_UNTRUSTED_INPUT[role_type]}"
    )


def test_every_concrete_platform_role_is_covered_by_the_marking_table() -> None:
    """Guards against a new role landing with no explicit expectation here."""
    assert set(discover_concrete_platform_roles()) == set(_EXPECTED_UNTRUSTED_INPUT)
