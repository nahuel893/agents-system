"""Issue #127 — defense-in-depth follow-ups from the #75/PR 97 containment
review. Every gap here fails closed today (none is an exploitable bypass of
the containment guarantee `test_importer_folder_containment.py` proves); this
module hardens the loader against a raw, unguarded exception escaping in
place of `DefinitionError`/`FactoryError`, and proves the real load path
actually routes through the TOCTOU-safe reader instead of a plain path read.

Strict TDD: every test in this file is RED against the pre-#127 code and
GREEN once the corresponding fix lands.

- `_contained_folder` / `_extends_target`'s `Path.is_dir()` calls used to let
  a raw `PermissionError` escape when an ancestor directory lacks search
  permission (a chmod-000 folder above the target, not the target itself —
  `os.stat()` only needs search permission on a path's ANCESTORS, never on
  the entry itself).
- `_read_within_root` had no size cap, opened directory handles with
  `O_RDONLY` (needing LIST permission, not just search/execute), accepted an
  unresolved path with a lexical `.`/`..` component, and let a raw
  `UnicodeDecodeError` escape for non-UTF-8 content.
- The existing TOCTOU/FIFO tests call `_read_within_root` directly; nothing
  proved the real callers (`_read_contained_md` via `resolve()`, `_read_skill`
  via `_load_skills`) actually route through it.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from agents_system.harness import factory as factory_module
from agents_system.harness import loader as loader_module
from agents_system.harness.factory import FactoryError, _load_skills
from agents_system.harness.loader import (
    _DIR_FD_READS,
    DefinitionError,
    FolderLocator,
    RootConfig,
    _read_within_root,
    resolve,
)

_needs_dir_fd = pytest.mark.skipif(
    not _DIR_FD_READS, reason="directory-handle reads need os.open(dir_fd=...)"
)


def _agent(
    base: pathlib.Path,
    name: str,
    *,
    extends: str | None = None,
    skills: tuple[str, ...] = (),
) -> pathlib.Path:
    """Write a minimal, well-formed agent folder under ``base`` (mirrors
    `test_importer_folder_containment.py`'s helper of the same shape)."""
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n{name} prose\n',
        encoding="utf-8",
    )
    extends_line = f"extends: {extends}\n" if extends is not None else ""
    (folder / "manifest.md").write_text(
        f"---\nrole: {name}\n{extends_line}tools: []\nskills: {list(skills)}\n"
        "context: {}\npermissions: []\n---\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {name}\nautonomy: supervised\nexecution_limits: null\n---\n",
        encoding="utf-8",
    )
    skills_dir = folder / "skills"
    skills_dir.mkdir()
    for skill in skills:
        (skills_dir / f"{skill}.md").write_text(f"{skill} content.\n", encoding="utf-8")
    return folder


def _roots(tmp_path: pathlib.Path) -> RootConfig:
    return RootConfig(
        platform_root=tmp_path / "platform", deployments_root=tmp_path / "deployments"
    )


class _RestoreMode:
    """Restores ``path``'s mode on exit, so a chmod-000 test folder does not
    break `tmp_path`'s own teardown (which needs to walk back into it)."""

    def __init__(self, path: pathlib.Path, mode: int) -> None:
        self._path = path
        self._mode = mode

    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        os.chmod(self._path, self._mode)


# ---------------------------------------------------------------------------
# `Path.is_dir()` unguarded in `_contained_folder` / `_extends_target`
# ---------------------------------------------------------------------------


def test_folder_ancestor_without_search_permission_is_a_definition_error(
    tmp_path: pathlib.Path,
) -> None:
    """`_contained_folder`'s `folder.is_dir()` used to let a raw
    `PermissionError` escape when an ancestor directory of an otherwise
    perfectly legitimate agent folder lacks search (execute) permission —
    e.g. an intermediate folder momentarily chmod'd 000. `os.stat()` needs
    search permission on every ANCESTOR of the path, never on the entry
    itself, so this is not about the agent folder's own permissions."""
    root = tmp_path / "agents"
    group = root / "group"
    folder = _agent(group, "vip-support")

    os.chmod(group, 0o000)
    with (
        _RestoreMode(group, 0o755),
        pytest.raises(DefinitionError, match="vip-support"),
    ):
        resolve(FolderLocator(path=folder, root=root), roots=_roots(tmp_path))


def test_extends_platform_role_ancestor_without_search_permission_is_a_definition_error(
    tmp_path: pathlib.Path,
) -> None:
    """The platform-role branch of `_extends_target` has its own unguarded
    `folder.is_dir()` — a locked platform `roles/` folder must not leak a raw
    `PermissionError` past the loader's `DefinitionError` contract."""
    platform_root = tmp_path / "platform"
    roles_dir = platform_root / "roles"
    (roles_dir / "locked-role").mkdir(parents=True)
    root = tmp_path / "agents"
    child = _agent(root, "child", extends="platform/roles/locked-role")

    os.chmod(roles_dir, 0o000)
    with (
        _RestoreMode(roles_dir, 0o755),
        pytest.raises(DefinitionError, match="locked-role"),
    ):
        resolve(
            FolderLocator(path=child, root=root),
            roots=RootConfig(
                platform_root=platform_root,
                deployments_root=tmp_path / "deployments",
            ),
        )


def test_extends_target_ancestor_without_search_permission_is_a_definition_error(
    tmp_path: pathlib.Path,
) -> None:
    """The importer-relative branch of `_extends_target` has its own
    unguarded `resolved.is_dir()` — an `extends:` target reached through a
    momentarily locked intermediate folder must not leak a raw
    `PermissionError` either."""
    root = tmp_path / "agents"
    group = root / "locked-parent"
    _agent(group, "base-support")
    child = _agent(root, "child", extends="../locked-parent/base-support")

    os.chmod(group, 0o000)
    with (
        _RestoreMode(group, 0o755),
        pytest.raises(DefinitionError, match="base-support"),
    ):
        resolve(FolderLocator(path=child, root=root), roots=_roots(tmp_path))


# ---------------------------------------------------------------------------
# `_read_within_root` has no size cap
# ---------------------------------------------------------------------------


def test_oversized_definition_file_is_refused(tmp_path: pathlib.Path) -> None:
    """A role/manifest/policy file above the loader's size cap must fail the
    load instead of forcing a full in-memory read at whatever size it
    reports — a sparse file can report a huge apparent size for almost no
    disk cost."""
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support")
    role_md = folder / "role.md"
    role_md.write_text(
        role_md.read_text(encoding="utf-8") + ("x" * (2 * 1024 * 1024)),
        encoding="utf-8",
    )

    with pytest.raises(DefinitionError, match="role.md") as excinfo:
        resolve(FolderLocator(path=folder, root=root), roots=_roots(tmp_path))

    assert "too large" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Directory walk needs LIST permission (O_RDONLY) instead of just search
# ---------------------------------------------------------------------------


@_needs_dir_fd
def test_search_only_folder_permission_still_loads(tmp_path: pathlib.Path) -> None:
    """A folder that grants only search (execute), no list/read, permission
    used to fail with a misleading "Permission denied" that read like a
    containment violation — `os.open(..., O_RDONLY|O_DIRECTORY)` needs list
    permission where `O_PATH|O_DIRECTORY` needs only search."""
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support")

    os.chmod(folder, 0o311)  # -wx for the owner: search, no read/list
    with _RestoreMode(folder, 0o755):
        definition = resolve(
            FolderLocator(path=folder, root=root), roots=_roots(tmp_path)
        )

    assert definition.role_name == "vip-support"


# ---------------------------------------------------------------------------
# `_read_within_root`'s own containment guard is lexical, not resolved
# ---------------------------------------------------------------------------


def test_read_within_root_rejects_a_lexical_dotdot_component(
    tmp_path: pathlib.Path,
) -> None:
    """`_read_within_root` is documented as safe only because every caller
    pre-resolves the path first: its own `is_relative_to` check is lexical
    and does not resolve `..`, so an UNRESOLVED path with a `..` component
    can walk out of `root` at read time even though the check appears to
    pass. This proves the escape exists today and must be closed."""
    root = tmp_path / "agents"
    _agent(root, "vip-support")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "role.md").write_text("OUTSIDE SECRET", encoding="utf-8")

    sneaky = root / "vip-support" / ".." / ".." / "outside" / "role.md"

    with pytest.raises(OSError):
        _read_within_root(root, sneaky)


# ---------------------------------------------------------------------------
# A non-UTF-8 file raises a raw UnicodeDecodeError instead of the loader's
# own error type
# ---------------------------------------------------------------------------


def test_non_utf8_role_file_is_a_definition_error(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support")
    (folder / "role.md").write_bytes(b"\xff\xfe not valid utf-8 \xff")

    with pytest.raises(DefinitionError, match="role.md"):
        resolve(FolderLocator(path=folder, root=root), roots=_roots(tmp_path))


def test_non_utf8_skill_file_is_reported_as_not_found(tmp_path: pathlib.Path) -> None:
    """`_read_skill` treats every read failure uniformly as "not found",
    exactly like an escaping symlink (issue #75's own documented choice,
    `_read_skill`'s docstring) — a non-UTF-8 skill file gets the same
    `FactoryError`, not a raw `UnicodeDecodeError`."""
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support", skills=("tone",))
    (folder / "skills" / "tone.md").write_bytes(b"\xff\xfe binary \xff")
    roots = _roots(tmp_path)
    definition = resolve(FolderLocator(path=folder, root=root), roots=roots)

    with pytest.raises(FactoryError, match="not found"):
        _load_skills(definition, None, roots)


# ---------------------------------------------------------------------------
# The real callers actually route through `_read_within_root`
# ---------------------------------------------------------------------------


def test_resolve_routes_role_files_through_read_within_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing previously asserted that `resolve()` reads role/manifest/policy
    files THROUGH `_read_within_root` rather than a plain path read — a
    regression reverting `_read_contained_md` to `path.read_text()` would
    pass the whole suite unnoticed (issue #127)."""
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support")
    original = loader_module._read_within_root
    seen: list[pathlib.Path] = []

    def spy(root_arg: pathlib.Path, path_arg: pathlib.Path) -> str:
        seen.append(path_arg)
        return original(root_arg, path_arg)

    monkeypatch.setattr(loader_module, "_read_within_root", spy)

    resolve(FolderLocator(path=folder, root=root), roots=_roots(tmp_path))

    seen_names = {path.name for path in seen}
    assert {"role.md", "manifest.md", "policy.md"} <= seen_names


def test_load_skills_routes_through_read_within_root(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same regression risk as above, for the skill-loading path."""
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support", skills=("tone",))
    roots = _roots(tmp_path)
    definition = resolve(FolderLocator(path=folder, root=root), roots=roots)

    original = factory_module._read_within_root
    seen: list[pathlib.Path] = []

    def spy(root_arg: pathlib.Path, path_arg: pathlib.Path) -> str:
        seen.append(path_arg)
        return original(root_arg, path_arg)

    monkeypatch.setattr(factory_module, "_read_within_root", spy)

    _load_skills(definition, None, roots)

    assert any(path.name == "tone.md" for path in seen)
