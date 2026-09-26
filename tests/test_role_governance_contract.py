"""Guard 1 contract tests (design.md D8; ``predefined-agent-governance``
spec): a predefined role's ``tools``/``permissions`` surface may only
diverge from its reviewed ``EXPECTED_ROLE_SURFACE`` snapshot when the change
carries BOTH a MAJOR ``version:`` bump and a ``CHANGELOG.md`` entry naming
the role.

The fixture-scoped tests below exercise ``check_role_governance_snapshot``
in isolation, with a caller-supplied ``changelog_text`` stand-in -- no real
``platform/roles/*/manifest.md`` edit and no real ``CHANGELOG.md`` write.
The single real-role test at the bottom is the one that actually runs
against the resolved, on-disk predefined roles and the real
``CHANGELOG.md`` on every PR.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest
from platform_role_contract import (
    EXPECTED_ROLE_SURFACE,
    EXPECTED_ROLE_TOOLS,
    PINNED_ROLES,
    RoleGovernanceSnapshot,
    check_role_governance_snapshot,
)

_FIXTURE_ROLE = "fixture-role"
_FIXTURE_SNAPSHOT = RoleGovernanceSnapshot(
    tools=frozenset({"session_state"}),
    permissions=frozenset({"read:session"}),
    version="1.0",
)


# ---------------------------------------------------------------------------
# "An unmodified predefined role matches its snapshot" /
# "A non-tools, non-permissions manifest edit does not trigger the guard"
# ---------------------------------------------------------------------------


def test_unmodified_role_matches_its_snapshot() -> None:
    """No tools/permissions divergence -> no requirement of any kind, even
    with an empty changelog and an unbumped version."""
    check_role_governance_snapshot(
        _FIXTURE_ROLE,
        _FIXTURE_SNAPSHOT.tools,
        _FIXTURE_SNAPSHOT.permissions,
        _FIXTURE_SNAPSHOT.version,
        _FIXTURE_SNAPSHOT,
        changelog_text="",
    )


def test_role_md_only_prose_edit_triggers_no_version_or_changelog_requirement() -> None:
    """A role's prose (``role.md``) is not part of ``RoleGovernanceSnapshot``
    at all -- the same unchanged-surface call the previous test makes is the
    only shape a prose-only edit can ever produce here, and it still passes
    with nothing bumped and nothing logged."""
    check_role_governance_snapshot(
        _FIXTURE_ROLE,
        _FIXTURE_SNAPSHOT.tools,
        _FIXTURE_SNAPSHOT.permissions,
        _FIXTURE_SNAPSHOT.version,
        _FIXTURE_SNAPSHOT,
        changelog_text="",
    )


# ---------------------------------------------------------------------------
# "The snapshot does not grade a role against itself"
# ---------------------------------------------------------------------------


def test_snapshot_does_not_grade_a_role_against_itself() -> None:
    """The "currently resolved" tools are a plain argument, independent of
    ``_FIXTURE_SNAPSHOT`` -- a divergent resolved value is caught even
    though nothing about the snapshot fixture itself changed."""
    resolved_tools = _FIXTURE_SNAPSHOT.tools | {"new_tool"}

    with pytest.raises(AssertionError, match="new_tool"):
        check_role_governance_snapshot(
            _FIXTURE_ROLE,
            resolved_tools,
            _FIXTURE_SNAPSHOT.permissions,
            _FIXTURE_SNAPSHOT.version,
            _FIXTURE_SNAPSHOT,
            changelog_text="",
        )


# ---------------------------------------------------------------------------
# "A contract check fails on any tools/permissions divergence lacking both a
# version bump and a CHANGELOG entry" (all four scenarios)
# ---------------------------------------------------------------------------


def test_divergence_with_neither_condition_fails_naming_both() -> None:
    with pytest.raises(AssertionError) as excinfo:
        check_role_governance_snapshot(
            _FIXTURE_ROLE,
            _FIXTURE_SNAPSHOT.tools | {"new_tool"},
            _FIXTURE_SNAPSHOT.permissions,
            _FIXTURE_SNAPSHOT.version,  # unchanged -- no MAJOR bump
            _FIXTURE_SNAPSHOT,
            changelog_text="",  # no entry
        )
    message = str(excinfo.value)
    assert _FIXTURE_ROLE in message
    assert "new_tool" in message
    assert "version bump" in message
    assert "CHANGELOG" in message


def test_divergence_with_version_bump_but_no_changelog_still_fails() -> None:
    with pytest.raises(AssertionError) as excinfo:
        check_role_governance_snapshot(
            _FIXTURE_ROLE,
            _FIXTURE_SNAPSHOT.tools | {"new_tool"},
            _FIXTURE_SNAPSHOT.permissions,
            "2.0",  # MAJOR bump over the snapshot's "1.0"
            _FIXTURE_SNAPSHOT,
            changelog_text="",  # no entry naming fixture-role anywhere
        )
    message = str(excinfo.value)
    assert "CHANGELOG" in message
    assert "version bump" not in message


def test_divergence_with_changelog_but_no_version_bump_still_fails() -> None:
    with pytest.raises(AssertionError) as excinfo:
        check_role_governance_snapshot(
            _FIXTURE_ROLE,
            _FIXTURE_SNAPSHOT.tools,
            _FIXTURE_SNAPSHOT.permissions | {"write:new_thing"},
            _FIXTURE_SNAPSHOT.version,  # unchanged
            _FIXTURE_SNAPSHOT,
            changelog_text=f"### {_FIXTURE_ROLE} gained write:new_thing",
        )
    message = str(excinfo.value)
    assert "version bump" in message
    assert "CHANGELOG" not in message


def test_divergence_with_both_conditions_passes() -> None:
    check_role_governance_snapshot(
        _FIXTURE_ROLE,
        _FIXTURE_SNAPSHOT.tools | {"new_tool"},
        _FIXTURE_SNAPSHOT.permissions,
        "2.0",
        _FIXTURE_SNAPSHOT,
        changelog_text=f"### {_FIXTURE_ROLE} gained new_tool",
    )


def test_removed_tools_and_permissions_are_also_named_on_divergence() -> None:
    """Not just additions -- a role that LOST a tool or permission is just
    as much a divergence as one that gained one."""
    with pytest.raises(AssertionError) as excinfo:
        check_role_governance_snapshot(
            _FIXTURE_ROLE,
            frozenset(),  # lost session_state
            frozenset(),  # lost read:session
            _FIXTURE_SNAPSHOT.version,
            _FIXTURE_SNAPSHOT,
            changelog_text="",
        )
    message = str(excinfo.value)
    assert "session_state" in message
    assert "read:session" in message


# ---------------------------------------------------------------------------
# "Guard 1 applies only to roles discovered under platform_root/roles" (both
# scenarios)
# ---------------------------------------------------------------------------


def test_expected_role_surface_covers_exactly_the_pinned_roles() -> None:
    """``EXPECTED_ROLE_SURFACE`` is keyed by exactly ``PINNED_ROLES`` -- the
    same discovery set ``EXPECTED_ROLE_TOOLS`` already uses, and
    ``test_platform_roles_on_disk_match_the_pinned_contract`` (in
    ``tests/test_platform_tools_integration.py``) already proves that set
    tracks ``platform/roles/`` on disk. Guard 1 reuses it rather than
    re-deriving a second, potentially drifting, discovery of its own."""
    assert set(EXPECTED_ROLE_SURFACE) == set(PINNED_ROLES)
    assert set(EXPECTED_ROLE_SURFACE) == set(EXPECTED_ROLE_TOOLS)


def test_importer_agent_extending_a_predefined_role_is_out_of_guard1_scope() -> None:
    """An importer-defined ``Agent`` extending ``sales-agent`` and adding its
    own tool on top is a real, resolvable agent -- but it is not, and can
    never become, a Guard 1 subject: it has no name in ``PINNED_ROLES`` or
    ``EXPECTED_ROLE_SURFACE``, the only set the real contract test below
    iterates."""
    from agents_system.agent.spec import Agent
    from agents_system.harness.loader import RootConfig, resolve

    importer = Agent(
        name="vip-sales",
        extends="platform/roles/sales-agent",
        tools=("vip_perk",),
        permissions=("read:catalog",),
    )

    definition = resolve(importer._to_locator(), roots=RootConfig())

    assert "vip_perk" in definition.tools  # the importer's own change is real
    assert "vip-sales" not in PINNED_ROLES
    assert "vip-sales" not in EXPECTED_ROLE_SURFACE


# ---------------------------------------------------------------------------
# The real contract test: runs on every PR against every predefined role's
# actual resolved surface and the real CHANGELOG.md.
# ---------------------------------------------------------------------------


def _changelog_text() -> str:
    changelog_path = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    return changelog_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("role", PINNED_ROLES)
def test_predefined_role_matches_its_governance_snapshot(role: str) -> None:
    from agents_system.harness import loader

    definition = loader.resolve(role, client=None)
    check_role_governance_snapshot(
        role,
        frozenset(definition.tools),
        frozenset(definition.permissions),
        definition.version,
        EXPECTED_ROLE_SURFACE[role],
        _changelog_text(),
    )


def test_no_role_governance_snapshot_reads_role_md_prose() -> None:
    """`RoleGovernanceSnapshot` is a plain, frozen (tools, permissions,
    version) value -- confirms it carries no field a prose-only edit could
    ever touch, so a `role.md` change literally cannot appear as a
    divergence."""
    fields = {f.name for f in dataclasses.fields(RoleGovernanceSnapshot)}
    assert fields == {"tools", "permissions", "version"}
