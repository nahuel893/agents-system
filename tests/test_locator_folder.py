"""Tests for `RoleLocator`/`FolderLocator` (design.md D1) and the folder-locator
dispatch branch of `_load_role_files`/`resolve` (PR1a).

Tests follow strict TDD: written BEFORE the implementation, intentionally fail
until `FolderLocator`/`RoleLocator` exist and `_load_role_files`/`resolve`
dispatch on them.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from agents_system.harness.loader import (
    AgentDefinition,
    DefinitionError,
    FolderLocator,
    RawDefinition,
    RoleLocator,
    RootConfig,
    _load_role_files,
    resolve,
)


def _write_folder(
    base: pathlib.Path,
    name: str,
    *,
    extends: str | None = None,
    abstract: bool = False,
) -> pathlib.Path:
    """Write a minimal, well-formed importer-agent folder: role.md/manifest.md/policy.md."""
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n# Role: {name}\n\nProse body.\n',
        encoding="utf-8",
    )
    extends_line = f"extends: {extends}\n" if extends else ""
    abstract_line = "abstract: true\n" if abstract else ""
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\n{extends_line}{abstract_line}'
        "tools: [tool_x]\nskills: []\ncontext: {}\npermissions:\n  - read:x\n"
        "---\n\nManifest body.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nautonomy: supervised\n'
        "execution_limits: null\n---\n\nPolicy body.\n",
        encoding="utf-8",
    )
    return folder


# ---------------------------------------------------------------------------
# PR1a-T1 — FolderLocator + RoleLocator shape
# ---------------------------------------------------------------------------


def test_folder_locator_is_frozen(tmp_path: pathlib.Path) -> None:
    locator = FolderLocator(path=tmp_path, root=tmp_path)
    with pytest.raises(dataclasses.FrozenInstanceError):
        locator.path = tmp_path / "elsewhere"  # type: ignore[misc]


def test_role_locator_is_importable_from_harness_loader() -> None:
    from agents_system.harness.loader import RoleLocator as ImportedRoleLocator

    assert ImportedRoleLocator is RoleLocator


def test_bare_str_locator_isinstance_matches_role_locator() -> None:
    """D1's own rejected-alternative rationale: a bare `str` needs no wrapping
    to be a valid `RoleLocator` — asserted directly so a future refactor
    cannot silently reintroduce a wrapper class."""
    assert isinstance("sales-agent", RoleLocator)


# ---------------------------------------------------------------------------
# PR1a-T2 — `_load_role_files` FolderLocator dispatch
# ---------------------------------------------------------------------------


def test_folder_locator_resolves_like_a_platform_role_folder(
    tmp_path: pathlib.Path,
) -> None:
    folder = _write_folder(tmp_path, "support-bot")
    locator = FolderLocator(path=folder, root=tmp_path)

    definition, parent, is_abstract = _load_role_files(locator, RootConfig())

    assert isinstance(definition, RawDefinition)
    assert definition.role_name == "support-bot"
    assert definition.tools == ["tool_x"]
    assert parent is None
    assert is_abstract is False


def test_folder_locator_missing_policy_raises_naming_file_and_folder(
    tmp_path: pathlib.Path,
) -> None:
    folder = _write_folder(tmp_path, "broken-bot")
    (folder / "policy.md").unlink()
    locator = FolderLocator(path=folder, root=tmp_path)

    with pytest.raises(DefinitionError) as excinfo:
        _load_role_files(locator, RootConfig())

    message = str(excinfo.value)
    assert "policy.md" in message
    assert str(folder) in message


def test_abstract_folder_agent_round_trips_is_abstract(
    tmp_path: pathlib.Path,
) -> None:
    """design.md's Open Question: `_load_role_files`'s FolderLocator branch
    reads `is_abstract` from the folder's own manifest identically to the
    platform branch — proven directly here, ahead of PR1a-T4 wiring
    `resolve()`'s rejection of an abstract role through it."""
    folder = _write_folder(tmp_path, "abstract-bot", abstract=True)
    locator = FolderLocator(path=folder, root=tmp_path)

    _definition, _parent, is_abstract = _load_role_files(locator, RootConfig())

    assert is_abstract is True


# ---------------------------------------------------------------------------
# PR1a-T4 — `resolve()`/`load_generic()` accept a FolderLocator
# ---------------------------------------------------------------------------


def test_resolve_folder_locator_end_to_end(tmp_path: pathlib.Path) -> None:
    folder = _write_folder(tmp_path, "support-bot")
    locator = FolderLocator(path=folder, root=tmp_path)
    roots = RootConfig(
        platform_root=tmp_path / "unused-platform",
        deployments_root=tmp_path / "unused-deployments",
    )

    definition = resolve(locator, client=None, roots=roots)

    assert isinstance(definition, AgentDefinition)
    assert definition.role_name == "support-bot"
    assert definition.tools == ("tool_x",)
    assert definition.autonomy == "supervised"


def test_resolve_folder_locator_with_client_raises(tmp_path: pathlib.Path) -> None:
    """Q5 (design.md D1): `client` is only valid with a `str` locator — a
    folder- or inline-sourced agent has no deployment tree to look one up in."""
    folder = _write_folder(tmp_path, "support-bot")
    locator = FolderLocator(path=folder, root=tmp_path)
    roots = RootConfig(
        platform_root=tmp_path / "unused-platform",
        deployments_root=tmp_path / "unused-deployments",
    )

    with pytest.raises(DefinitionError):
        resolve(locator, client="acme", roots=roots)
