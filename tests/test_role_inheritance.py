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

`autonomy` and `execution_limits` are the child's to declare, in EITHER
direction. An earlier version of this file asserted them as ceilings here,
reusing the deployment-path validators. Building the taxonomy disproved it
within minutes: `data-agent` runs `autonomy: full` and could not descend from
a `supervised` base, so the rule made the hierarchy unusable for any role that
legitimately runs unsupervised.

The subtractive rule exists because a deployment is authored by someone else.
Between two platform roles there is no second party, so a child declaring
`full` is a design decision rather than an escalation, and refusing it bought
no safety. The ceiling that matters is unchanged and asserted below.
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


def test_a_child_declares_its_own_execution_limits() -> None:
    """Between two platform roles, the child's limits win.

    `fx-strict` caps the turn at 10s; `fx-loosens-limits` asks for 999. Both
    files are written by the same author, so this is a design decision about
    one role, not an escape from a ceiling somebody else set. The ceiling that
    matters is at the deployment edge, asserted below.
    """
    definition = resolve("fx-loosens-limits", roots=_roots())

    assert definition.execution_limits is not None
    assert definition.execution_limits["total_execution_timeout_s"] == 999


def test_a_child_declares_its_own_autonomy() -> None:
    """A platform role may run unsupervised even under a supervised parent.

    Without this, an analytics role that legitimately needs `full` cannot
    descend from a conversational base — which is exactly the case that
    disproved the earlier, stricter rule.
    """
    definition = resolve("fx-loosens-autonomy", roots=_roots())

    assert definition.autonomy == "full"


def test_a_deployment_still_may_not_loosen_the_resolved_role() -> None:
    """The subtractive rule, where it actually belongs.

    Relaxing role-to-role composition must not relax this. A deployment is
    authored by a different party, so it may only narrow — and now it is
    measured against the FULLY RESOLVED chain rather than the leaf manifest,
    which is strictly stronger than before.
    """
    fixtures = pathlib.Path(__file__).parent / "fixtures" / "agents"
    roots = RootConfig(
        platform_root=fixtures / "generic-role",
        deployments_root=fixtures / "overrides" / "deployments",
    )

    with pytest.raises(DefinitionError) as excinfo:
        resolve("simple-role", client="bad-autonomy", roots=roots)

    assert "autonomy" in str(excinfo.value).lower()


# --- The declaration is no longer inert ------------------------------------


def test_extends_names_a_parent_the_directory_convention_would_not_pick() -> None:
    """The regression this whole change exists to fix, tested un-fakeably.

    This asserted `"extends" in loader.__file__`'s source text. That could
    not fail: the substring survives in a function name and three docstrings,
    so it passed with the directive gutted — the exact regression it claimed
    to catch.

    Behaviour alone is not enough either, because a directory convention that
    happens to agree would satisfy it. So the fixture is built the one way
    both are excluded: `fx-mid` lives in a folder named `fx-mid` and declares
    `extends: platform/roles/fx-base`. Nothing about the path says `fx-base`.
    If the directive is not read, `read:base` cannot arrive.
    """
    definition = resolve("fx-mid", roots=_roots())

    assert "read:base" in definition.permissions, (
        "fx-mid inherited nothing from fx-base, so `extends:` was not read"
    )


def test_execution_limits_inherit_down_the_chain() -> None:
    """A child that declares none must keep its parent's ceiling.

    Untested until now, and the consequence was concrete: deleting the
    inheritance line silently strips `developer-agent` — the only role that
    can run host commands — of `operator-agent`'s 30s / 10-call ceiling, and
    nothing goes red. It would then fall back to the platform defaults, which
    are twice as loose.
    """
    strict = resolve("fx-strict", roots=_roots())
    assert strict.execution_limits is not None

    # fx-loosens-autonomy extends fx-strict and declares no limits of its own.
    child = resolve("fx-loosens-autonomy", roots=_roots())

    assert child.execution_limits == strict.execution_limits, (
        "the child dropped its parent's ceiling by saying nothing"
    )


def test_a_deployment_declaring_one_limit_keeps_the_roles_others(
    tmp_path: pathlib.Path,
) -> None:
    """Declaring a STRICTER limit must not raise the ceiling on every other.

    `_merge_validated` replaced the role's `execution_limits` dict with the
    deployment's instead of merging over it, and `_effective_limits` backfills
    missing keys from the PLATFORM defaults rather than the role. So a
    deployment naming one tighter value silently doubled the rest:

        operator-agent   tool_call 10  total 30  max_calls 10
        deployment says  tool_call 1
        effective        tool_call 1   total 60  max_calls 20

    On the only role that can run host commands. The validator passed,
    because every key the deployment DECLARED really was stricter — the
    escape is in the keys it did not declare.
    """
    from agentsys.harness.loader import RootConfig, resolve

    dep = tmp_path / "sneaky" / "operator-agent"
    dep.mkdir(parents=True)
    (dep / "role.md").write_text("---\nname: operator-agent\n---\n\nbody\n")
    (dep / "manifest.md").write_text(
        "---\nrole: operator-agent\ndeployment: sneaky\ntools: [use_term]\n"
        "skills: []\ncontext: {}\npermissions: inherit\n---\n\nm\n"
    )
    (dep / "policy.md").write_text(
        "---\nrole: operator-agent\nautonomy: supervised\n"
        "execution_limits:\n  tool_call_timeout_s: 1\n---\n\np\n"
    )

    role = resolve("operator-agent")
    deployment = resolve(
        "operator-agent", client="sneaky", roots=RootConfig(deployments_root=tmp_path)
    )

    assert role.execution_limits is not None
    assert deployment.execution_limits is not None
    # The declared one narrowed...
    assert deployment.execution_limits["tool_call_timeout_s"] == 1
    # ...and every undeclared one kept the ROLE's value, not the platform's.
    for key in ("total_execution_timeout_s", "max_tool_calls"):
        assert deployment.execution_limits[key] == role.execution_limits[key], (
            f"'{key}' escaped the role ceiling: "
            f"{deployment.execution_limits[key]} vs {role.execution_limits[key]}"
        )


def test_a_deployed_agent_still_sees_its_platform_roles_prose() -> None:
    """The composed chain must survive the deployment merge.

    `_merge_validated` set `system_prompt = override.system_prompt`, throwing
    away everything the chain had folded — so a deployed agent never saw its
    platform role's prose, and after the taxonomy landed, never saw `base`'s
    or `agent`'s either. Measured before the fix: the resolved role's prompt
    was 2698 characters and the deployment's 780.

    That is not cosmetic. The standing instructions those files carry —
    "report what ran verbatim", "a confident zero is worse than an error" —
    existed only for a role resolved WITHOUT a deployment, which is not how
    anything runs.
    """
    from agentsys.harness.loader import resolve as real_resolve

    role = real_resolve("sales-agent")
    deployed = real_resolve("sales-agent", client="acme")

    # Every non-empty line of the role's prose survives into the deployment's.
    missing = [
        line
        for line in role.system_prompt.splitlines()
        if line.strip() and line not in deployed.system_prompt
    ]
    assert not missing, f"the deployment dropped {len(missing)} lines of role prose"

    # And the deployment's own prose is still there, after it.
    assert len(deployed.system_prompt) > len(role.system_prompt)


def test_a_deployment_may_not_declare_a_parent_it_does_not_have(
    tmp_path: pathlib.Path,
) -> None:
    """`extends:` in a deployment manifest was authoritative nowhere.

    A deployment's parent is fixed by its directory, so `extends:` here
    cannot choose anything — and `load_override` never read it. The same key
    was therefore authoritative in a role manifest and silently inert in a
    deployment one, which is worse than uniformly ignored: a contradiction
    reads as a decision and does nothing.

    Declaring the truth stays legal. Declaring a lie now fails.
    """
    from agentsys.harness.loader import DefinitionError, RootConfig, load_override

    dep = tmp_path / "acme" / "sales-agent"
    dep.mkdir(parents=True)
    (dep / "role.md").write_text("---\nname: sales-agent\n---\n\nbody\n")
    (dep / "manifest.md").write_text(
        "---\nrole: sales-agent\ndeployment: acme\n"
        "extends: platform/roles/orchestrator\n"  # <- not its own role
        "tools: []\nskills: []\ncontext: {}\npermissions: inherit\n---\n\nm\n"
    )
    (dep / "policy.md").write_text(
        "---\nrole: sales-agent\nautonomy: supervised\nexecution_limits: null\n---\n\np\n"
    )

    with pytest.raises(DefinitionError) as excinfo:
        load_override("acme", "sales-agent", roots=RootConfig(deployments_root=tmp_path))

    message = str(excinfo.value)
    assert "orchestrator" in message
    assert "sales-agent" in message
