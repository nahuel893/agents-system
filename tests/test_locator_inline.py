"""Tests for `RoleLocator`/`InlineLocator` (design.md D1) and the inline-locator
dispatch branch of `_load_role_files`/`_resolve_role_chain` (PR1a).

Tests follow strict TDD: written BEFORE the implementation, intentionally fail
until `InlineLocator`/`RoleLocator` exist and the loader dispatches on them.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any

import pytest

from agents_system.harness.loader import (
    DefinitionError,
    InlineLocator,
    RawDefinition,
    RootConfig,
    _load_role_files,
    _resolve_role_chain,
    resolve,
)


def _raw(**overrides: Any) -> RawDefinition:
    """Build a RawDefinition with sensible defaults, overriding chosen fields."""
    base: dict[str, Any] = {
        "role_name": "role",
        "version": "1.0",
        "deployment": None,
        "system_prompt": "",
        "tools": [],
        "skills": [],
        "context": {},
        "permissions": [],
        "autonomy": "supervised",
        "escalation_rules": {},
        "delegation_policy": {},
        "memory_policy": {},
        "audit_policy": {},
        "execution_limits": None,
    }
    base.update(overrides)
    return RawDefinition(**base)


# ---------------------------------------------------------------------------
# PR1a-T1 — InlineLocator shape
# ---------------------------------------------------------------------------


def test_inline_locator_is_frozen() -> None:
    locator = InlineLocator(raw=_raw(), parent=None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        locator.parent = "agent"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# PR1a-T2 — `_load_role_files` InlineLocator dispatch + platform regression
# ---------------------------------------------------------------------------


def test_inline_locator_returns_raw_unchanged_with_no_disk_io(
    tmp_path: pathlib.Path,
) -> None:
    """An InlineLocator skips disk entirely — proven by pointing
    `platform_root` at a directory that does not exist anywhere on disk and
    confirming no attempt to read from it is ever made."""
    raw = _raw(role_name="inline-agent")
    locator = InlineLocator(raw=raw, parent=None)
    roots = RootConfig(
        platform_root=tmp_path / "does-not-exist",
        deployments_root=tmp_path / "also-does-not-exist",
    )

    definition, parent, is_abstract = _load_role_files(locator, roots)

    assert definition is raw
    assert parent is None
    assert is_abstract is False


def test_platform_branch_regression_byte_identical_output(
    tmp_path: pathlib.Path,
) -> None:
    """Regression: the existing `isinstance(locator, str)` platform branch
    must produce byte-identical output to before this change, for every
    existing fixture role."""
    fixture_roots = RootConfig(
        platform_root=pathlib.Path(__file__).parent
        / "fixtures"
        / "agents"
        / "generic-role",
        deployments_root=tmp_path,
    )

    definition, parent, is_abstract = _load_role_files("simple-role", fixture_roots)

    assert definition.role_name == "simple-role"
    assert set(definition.tools) == {"tool_alpha", "tool_beta", "tool_gamma"}
    assert set(definition.permissions) == {"read:alpha", "read:beta", "write:gamma"}
    assert parent is None
    assert is_abstract is False


# ---------------------------------------------------------------------------
# PR1a-T3 — `_resolve_role_chain` locator-keyed cycle detection + chain walk
# ---------------------------------------------------------------------------


def _write_platform_role(
    roles_dir: pathlib.Path, name: str, *, extends: str | None
) -> None:
    folder = roles_dir / name
    folder.mkdir(parents=True)
    extends_line = f"\nextends: {extends}" if extends else ""
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\nbody\n', encoding="utf-8"
    )
    (folder / "manifest.md").write_text(
        f"---\nrole: {name}{extends_line}\ntools: []\nskills: []\n"
        "context: {}\npermissions: []\n---\n\nmanifest\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {name}\nautonomy: supervised\nexecution_limits: null\n"
        "---\n\npolicy\n",
        encoding="utf-8",
    )


def test_inline_locator_cycle_via_platform_name_raises_keyed_by_locator(
    tmp_path: pathlib.Path,
) -> None:
    """The chain walk operates over `RoleLocator` values (D1's shared
    resolution pipeline): an inline locator's (not-yet-resolved) `parent` may
    point into platform-role `str` space, and a cycle reached that way is
    still detected — keyed by `_locator_key`'s `f"platform:{name}"` shape,
    not the old bare-string `seen` list."""
    roles_dir = tmp_path / "roles"
    _write_platform_role(roles_dir, "self-loop", extends="self-loop")
    roots = RootConfig(platform_root=tmp_path)

    raw = _raw(role_name="inline-agent")
    locator = InlineLocator(raw=raw, parent="self-loop")

    with pytest.raises(DefinitionError) as excinfo:
        _resolve_role_chain(locator, roots)

    message = str(excinfo.value)
    assert "cycle" in message.lower()
    assert "platform:self-loop" in message


def test_locator_based_chain_still_hits_the_depth_ceiling(
    tmp_path: pathlib.Path,
) -> None:
    """`_MAX_ROLE_CHAIN_DEPTH` (8) still fires identically for a chain that
    starts from a non-`str` locator."""
    from agents_system.harness import loader

    depth = loader._MAX_ROLE_CHAIN_DEPTH + 2
    roles_dir = tmp_path / "roles"
    for i in range(depth):
        parent = f"deep-{i + 1}" if i < depth - 1 else None
        _write_platform_role(roles_dir, f"deep-{i}", extends=parent)
    roots = RootConfig(platform_root=tmp_path)

    raw = _raw(role_name="inline-root")
    locator = InlineLocator(raw=raw, parent="deep-0")

    with pytest.raises(DefinitionError) as excinfo:
        _resolve_role_chain(locator, roots)

    message = str(excinfo.value)
    assert "deeper than" in message
    assert "cycle" not in message.lower()


def test_normal_platform_chain_regression_sales_agent_extends_agent() -> None:
    """Regression: `_resolve_role_chain`'s locator-keyed rewrite must not
    change a normal, pure-`str` platform-role chain's resolved output.
    `escalation_notifier` is declared only by `agent`, never by
    `sales-agent` itself, so its presence proves the fold still walked and
    composed the inherited parent correctly."""
    definition = resolve("sales-agent", roots=RootConfig())

    assert definition.role_name == "sales-agent"
    assert "escalation_notifier" in definition.tools
