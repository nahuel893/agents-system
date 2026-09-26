"""Issue #75 — every read from an importer agent folder stays inside its
importer root, and nothing an importer supplies skips the loader's checks.

- role.md, manifest.md, policy.md and skills/ files resolve inside the
  importer root (symlinks at any level included), or the load fails.
- The leaf ``FolderLocator.path`` is checked against its own ``root``.
- Reads walk from the root with ``O_NOFOLLOW`` directory handles, so a
  folder swapped for a symlink after the check is refused (TOCTOU).
- ``FolderLocator.overrides`` only applies the fields ``Agent`` can override.
- ``resolve()`` re-validates every command tool declaration, whichever
  locator built it (an ``InlineLocator`` skips the manifest parser).
- The deployment skills path is contained in the deployments root.
- Loader errors name files relative to their root, never the host path.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
from typing import Any

import pytest

from agents_system.agent.spec import _AGENT_OVERRIDABLE_FIELDS, Agent
from agents_system.harness.factory import FactoryError, _load_skills
from agents_system.harness.loader import (
    _DIR_FD_READS,
    _FOLDER_OVERRIDE_FIELDS,
    AgentDefinition,
    CommandToolDeclaration,
    CommandToolParam,
    DefinitionError,
    FolderLocator,
    InlineLocator,
    RawDefinition,
    RootConfig,
    _read_within_root,
    _resolve_within_root,
    resolve,
)
from agents_system.harness.registry import Tier

_ROLE_FILES = ("role.md", "manifest.md", "policy.md")


def _agent(
    base: pathlib.Path,
    name: str,
    *,
    extends: str | None = None,
    skills: tuple[str, ...] = (),
    manifest_extra: str = "",
    policy_extra: str = "",
    role_body: str = "prose",
) -> pathlib.Path:
    """Write a minimal, well-formed agent folder under ``base``."""
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n{name} {role_body}\n',
        encoding="utf-8",
    )
    extends_line = f"extends: {extends}\n" if extends is not None else ""
    (folder / "manifest.md").write_text(
        f"---\nrole: {name}\n{extends_line}tools: []\nskills: {list(skills)}\n"
        f"context: {{}}\npermissions: []\n{manifest_extra}---\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f"---\nrole: {name}\nautonomy: supervised\nexecution_limits: null\n"
        f"{policy_extra}---\n",
        encoding="utf-8",
    )
    skills_dir = folder / "skills"
    skills_dir.mkdir()
    for skill in skills:
        (skills_dir / f"{skill}.md").write_text(f"{skill} content.\n", encoding="utf-8")
    return folder


@pytest.fixture
def root(tmp_path: pathlib.Path) -> pathlib.Path:
    """An importer agents root holding `vip-support`."""
    agents = tmp_path / "agents"
    _agent(agents, "vip-support")
    return agents


@pytest.fixture
def outside(tmp_path: pathlib.Path) -> pathlib.Path:
    """A well-formed agent folder OUTSIDE the importer root, whose files are
    what an escaping symlink would read."""
    return _agent(tmp_path / "outside", "stolen", role_body="SECRET prose")


def _roots(tmp_path: pathlib.Path) -> RootConfig:
    return RootConfig(
        platform_root=tmp_path / "platform", deployments_root=tmp_path / "deployments"
    )


def _error(locator: Any, roots: RootConfig) -> str:
    with pytest.raises(DefinitionError) as excinfo:
        resolve(locator, roots=roots)
    return str(excinfo.value)


def _assert_no_host_path(message: str, tmp_path: pathlib.Path) -> None:
    assert str(tmp_path) not in message
    assert str(tmp_path.resolve()) not in message


# ---------------------------------------------------------------------------
# role.md / manifest.md / policy.md are contained
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("filename", _ROLE_FILES)
def test_role_file_symlinked_outside_the_root_is_rejected(
    root: pathlib.Path, outside: pathlib.Path, tmp_path: pathlib.Path, filename: str
) -> None:
    """Acceptance: a symlinked `manifest.md` (and its two siblings) that
    points outside the importer root fails the load instead of being read."""
    target = root / "vip-support" / filename
    target.unlink()
    os.symlink(outside / filename, target)

    message = _error(
        FolderLocator(path=root / "vip-support", root=root), _roots(tmp_path)
    )

    assert filename in message
    assert "vip-support" in message
    assert "importer root" in message
    _assert_no_host_path(message, tmp_path)


def test_role_file_symlinked_inside_the_root_is_followed(
    root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Containment is checked after following symlinks, as for `extends:`:
    a manifest shared from elsewhere inside the root is fine."""
    shared = _agent(root, "shared", manifest_extra="tools_note: shared\n")
    manifest = root / "vip-support" / "manifest.md"
    manifest.unlink()
    os.symlink(shared / "manifest.md", manifest)

    definition = resolve(
        FolderLocator(path=root / "vip-support", root=root), roots=_roots(tmp_path)
    )

    assert definition.role_name == "vip-support"


def test_intermediate_directory_symlinked_outside_the_root_is_rejected(
    root: pathlib.Path, outside: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A symlink at any level counts: `root/group -> <outside>` puts
    `root/group/stolen` outside the root even though its spelling is not."""
    os.symlink(outside.parent, root / "group")

    message = _error(
        FolderLocator(path=root / "group" / "stolen", root=root), _roots(tmp_path)
    )

    assert "importer root" in message
    _assert_no_host_path(message, tmp_path)


def test_parent_reached_through_extends_has_its_files_contained_too(
    root: pathlib.Path, outside: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    base = _agent(root, "base-support")
    (base / "policy.md").unlink()
    os.symlink(outside / "policy.md", base / "policy.md")
    child = _agent(root, "child", extends="../base-support")

    message = _error(FolderLocator(path=child, root=root), _roots(tmp_path))

    assert "policy.md" in message
    assert "importer root" in message
    _assert_no_host_path(message, tmp_path)


def test_skills_directory_symlinked_outside_the_root_is_not_read(
    tmp_path: pathlib.Path, outside: pathlib.Path
) -> None:
    """The PR3 check bounded a skill file by its own `skills/` folder only, so
    a `skills` directory that is itself a symlink out of the root passed."""
    (outside / "skills" / "tone.md").write_text("SECRET skill.\n", encoding="utf-8")
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support")
    manifest = folder / "manifest.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("skills: []", "skills: [tone]"),
        encoding="utf-8",
    )
    (folder / "skills").rmdir()
    os.symlink(outside / "skills", folder / "skills")
    roots = _roots(tmp_path)
    definition = resolve(FolderLocator(path=folder, root=root), roots=roots)

    with pytest.raises(FactoryError) as excinfo:
        _load_skills(definition, None, roots)

    _assert_no_host_path(str(excinfo.value), tmp_path)


def test_folder_skill_inside_the_root_still_loads(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support", skills=("tone",))
    roots = _roots(tmp_path)
    definition = resolve(FolderLocator(path=folder, root=root), roots=roots)

    (loaded,) = _load_skills(definition, None, roots)

    assert loaded.content == "tone content."


def test_missing_folder_skill_error_names_it_relative_to_the_root(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support")
    manifest = folder / "manifest.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("skills: []", "skills: [ghost]"),
        encoding="utf-8",
    )
    roots = _roots(tmp_path)
    definition = resolve(FolderLocator(path=folder, root=root), roots=roots)

    with pytest.raises(FactoryError) as excinfo:
        _load_skills(definition, None, roots)

    assert "vip-support/skills/ghost.md" in str(excinfo.value)
    _assert_no_host_path(str(excinfo.value), tmp_path)


# ---------------------------------------------------------------------------
# The leaf FolderLocator.path is checked against its root
# ---------------------------------------------------------------------------


def test_leaf_path_outside_its_root_is_rejected(
    root: pathlib.Path, outside: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    message = _error(FolderLocator(path=outside, root=root), _roots(tmp_path))

    assert "stolen" in message
    assert "importer root" in message
    _assert_no_host_path(message, tmp_path)


def test_leaf_path_escaping_through_dotdot_is_rejected(
    root: pathlib.Path, outside: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    escaping = root / ".." / "outside" / "stolen"

    message = _error(FolderLocator(path=escaping, root=root), _roots(tmp_path))

    assert "importer root" in message
    _assert_no_host_path(message, tmp_path)


def test_leaf_path_equal_to_its_root_is_rejected(
    root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    folder = root / "vip-support"

    message = _error(FolderLocator(path=folder, root=folder), _roots(tmp_path))

    assert "importer root" in message
    _assert_no_host_path(message, tmp_path)


def test_leaf_symlink_leaving_its_root_is_rejected(
    root: pathlib.Path, outside: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """`Agent.from_folder(path)` sets `root=path.parent`: a leaf folder that is
    a symlink to somewhere else is outside that root."""
    link = root / "linked"
    os.symlink(outside, link)

    with pytest.raises(DefinitionError, match="importer root"):
        resolve(Agent.from_folder(link)._to_locator(), roots=_roots(tmp_path))


def test_from_folder_current_directory_still_resolves(
    root: pathlib.Path, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Path(".").parent` is `Path(".")`: without a normalized root, the leaf
    check would call the current directory its own importer root."""
    monkeypatch.chdir(root / "vip-support")

    definition = resolve(Agent.from_folder(".")._to_locator(), roots=_roots(tmp_path))

    assert definition.role_name == "vip-support"


# ---------------------------------------------------------------------------
# TOCTOU — reads walk from the root with O_NOFOLLOW directory handles
# ---------------------------------------------------------------------------

_needs_dir_fd = pytest.mark.skipif(
    not _DIR_FD_READS, reason="directory-handle reads need os.open(dir_fd=...)"
)


@_needs_dir_fd
def test_folder_swapped_for_a_symlink_after_the_check_is_refused(
    root: pathlib.Path, outside: pathlib.Path
) -> None:
    """The path passed the containment check, then the folder was replaced by
    a symlink to outside it. A path-based read would follow it."""
    checked = _resolve_within_root(root, root / "vip-support", "manifest.md")
    assert checked is not None
    (root / "vip-support").rename(root / "moved")
    os.symlink(outside, root / "vip-support")
    assert "stolen" in checked.read_text(encoding="utf-8")  # what a path read gets

    with pytest.raises(OSError):
        _read_within_root(root, checked)


@_needs_dir_fd
def test_file_swapped_for_a_symlink_after_the_check_is_refused(
    root: pathlib.Path, outside: pathlib.Path
) -> None:
    checked = _resolve_within_root(root, root / "vip-support", "manifest.md")
    assert checked is not None
    checked.unlink()
    os.symlink(outside / "manifest.md", checked)

    with pytest.raises(OSError):
        _read_within_root(root, checked)


@_needs_dir_fd
def test_a_fifo_is_refused_without_blocking(root: pathlib.Path) -> None:
    """A named pipe opened for reading would block the load forever."""
    checked = _resolve_within_root(root, root / "vip-support", "manifest.md")
    assert checked is not None
    checked.unlink()
    os.mkfifo(checked)

    with pytest.raises(OSError):
        _read_within_root(root, checked)


def test_read_within_root_reads_a_contained_file(root: pathlib.Path) -> None:
    checked = _resolve_within_root(root, root / "vip-support", "role.md")
    assert checked is not None

    assert "vip-support prose" in _read_within_root(root, checked)


def test_read_within_root_refuses_a_path_outside_the_root(
    root: pathlib.Path, outside: pathlib.Path
) -> None:
    with pytest.raises(OSError):
        _read_within_root(root, outside / "role.md")


# ---------------------------------------------------------------------------
# FolderLocator.overrides — a loader-side allowlist
# ---------------------------------------------------------------------------


def test_loader_allowlist_matches_what_agent_can_override() -> None:
    assert set(_FOLDER_OVERRIDE_FIELDS) == set(_AGENT_OVERRIDABLE_FIELDS)


_T2_TOOL = CommandToolDeclaration(
    name="check_stock",
    argv=("/usr/bin/true", "{sku}"),
    params={"sku": CommandToolParam(type="string", pattern="^[A-Z0-9]+$")},
    tier=Tier.T2,
    permission="run:check_stock",
)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("command_tool_declarations", {"check_stock": _T2_TOOL}),
        ("command_tools", ["check_stock"]),
        ("deployment", "client-a"),
        ("skills_folder", pathlib.Path("/")),
        ("importer_root", pathlib.Path("/")),
        ("inline_skills", {"tone": "text"}),
        ("role_name", "renamed"),
    ],
)
def test_override_outside_the_allowlist_is_rejected(
    root: pathlib.Path, tmp_path: pathlib.Path, key: str, value: Any
) -> None:
    """A `FolderLocator` built directly used to apply ANY `RawDefinition`
    field, including command tool declarations and the deployment."""
    locator = FolderLocator(
        path=root / "vip-support", root=root, overrides={key: value}
    )

    message = _error(locator, _roots(tmp_path))

    assert key in message


def test_allowed_override_is_still_applied(
    root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    locator = FolderLocator(
        path=root / "vip-support",
        root=root,
        overrides={"name": "renamed", "tools": ["x"]},
    )

    definition = resolve(locator, roots=_roots(tmp_path))

    assert definition.role_name == "renamed"
    assert definition.tools == ("x",)


def test_untrusted_input_override_is_parsed_like_the_policy_file(
    root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    locator = FolderLocator(
        path=root / "vip-support", root=root, overrides={"untrusted_input": "false"}
    )

    assert "untrusted_input" in _error(locator, _roots(tmp_path))


# ---------------------------------------------------------------------------
# resolve() re-validates command tools for every locator kind
# ---------------------------------------------------------------------------


def _raw(**fields: Any) -> RawDefinition:
    base: dict[str, Any] = {
        "role_name": "inline-bot",
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
    base.update(fields)
    return RawDefinition(**base)


def _with_tool(declaration: Any, *, name: str = "check_stock") -> InlineLocator:
    return InlineLocator(
        raw=_raw(command_tools=[name], command_tool_declarations={name: declaration})
    )


def test_valid_inline_command_tool_resolves() -> None:
    definition = resolve(_with_tool(_T2_TOOL))

    assert definition.command_tools == (_T2_TOOL,)


def _replaced(**fields: Any) -> CommandToolDeclaration:
    return dataclasses.replace(_T2_TOOL, **fields)


@pytest.mark.parametrize(
    ("declaration", "reason"),
    [
        (_replaced(tier=Tier.T0), "T2"),
        (_replaced(tier=Tier.T1), "T2"),
        (_replaced(tier=Tier.T3), "T2"),
        (_replaced(argv=("true", "{sku}")), "absolute"),
        (_replaced(argv=("{sku}",)), "argv\\[0\\]"),
        (_replaced(argv=("/usr/bin/true", "--sku={sku}")), "WHOLE"),
        (_replaced(argv=("/usr/bin/true", "{other}")), "no matching param"),
        (_replaced(argv=("/usr/bin/true",)), "never appear"),
        (_replaced(argv=()), "non-empty"),
        (_replaced(permission="exec:command"), "run:"),
        (_replaced(params={"sku": CommandToolParam(type="string")}), "pattern"),
        (_replaced(params={"sku": {"type": "string"}}), "CommandToolParam"),
        ({"argv": ["/usr/bin/true"]}, "CommandToolDeclaration"),
    ],
)
def test_invalid_inline_command_tool_is_rejected_by_resolve(
    declaration: Any, reason: str
) -> None:
    """`InlineLocator(raw=RawDefinition(...))` never goes through the manifest
    parser, so each load-time rule is enforced again in `resolve()`."""
    with pytest.raises(DefinitionError, match=reason) as excinfo:
        resolve(_with_tool(declaration))

    assert "inline-bot" in str(excinfo.value)


def test_inline_command_tool_under_a_platform_parent_is_rejected() -> None:
    locator = InlineLocator(
        raw=_raw(
            command_tools=["check_stock"],
            command_tool_declarations={"check_stock": _replaced(tier=Tier.T3)},
        ),
        parent="agent",
    )

    with pytest.raises(DefinitionError, match="T2"):
        resolve(locator)


def test_command_tool_named_without_a_declaration_is_a_definition_error() -> None:
    locator = InlineLocator(raw=_raw(command_tools=["ghost"]))

    with pytest.raises(DefinitionError, match="ghost"):
        resolve(locator)


def test_declaration_filed_under_another_name_is_rejected() -> None:
    locator = InlineLocator(
        raw=_raw(command_tools=["alias"], command_tool_declarations={"alias": _T2_TOOL})
    )

    with pytest.raises(DefinitionError, match="alias"):
        resolve(locator)


# ---------------------------------------------------------------------------
# The deployment skills path is contained in the deployments root
# ---------------------------------------------------------------------------


def _deployment_definition(role_name: str, skill: str) -> AgentDefinition:
    return AgentDefinition(
        role_name=role_name,
        version="1.0",
        deployment="client-a",
        system_prompt="",
        tools=(),
        skills=(skill,),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )


@pytest.fixture
def deployment_skills(tmp_path: pathlib.Path) -> pathlib.Path:
    skills = tmp_path / "deployments" / "client-a" / "simple-role" / "skills"
    skills.mkdir(parents=True)
    (skills / "tone.md").write_text("Deployment tone.\n", encoding="utf-8")
    # What each escape below would reach: `<tmp>/secret.md` and
    # `<tmp>/skills/secret.md`, both outside the deployments root.
    (tmp_path / "secret.md").write_text("SECRET\n", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "secret.md").write_text("SECRET\n", encoding="utf-8")
    return skills


def test_deployment_skill_inside_the_root_still_loads(
    tmp_path: pathlib.Path, deployment_skills: pathlib.Path
) -> None:
    (loaded,) = _load_skills(
        _deployment_definition("simple-role", "tone"), "client-a", _roots(tmp_path)
    )

    assert loaded.content == "Deployment tone."


@pytest.mark.parametrize(
    ("role_name", "skill"),
    [
        ("simple-role", "../../../../secret"),
        ("../..", "secret"),
    ],
)
def test_deployment_skill_path_escaping_the_root_is_not_read(
    tmp_path: pathlib.Path, deployment_skills: pathlib.Path, role_name: str, skill: str
) -> None:
    with pytest.raises(FactoryError) as excinfo:
        _load_skills(
            _deployment_definition(role_name, skill), "client-a", _roots(tmp_path)
        )

    _assert_no_host_path(str(excinfo.value), tmp_path)


def test_deployment_skill_symlinked_out_of_the_root_is_not_read(
    tmp_path: pathlib.Path, deployment_skills: pathlib.Path
) -> None:
    os.symlink(tmp_path / "secret.md", deployment_skills / "leak.md")

    with pytest.raises(FactoryError):
        _load_skills(
            _deployment_definition("simple-role", "leak"), "client-a", _roots(tmp_path)
        )


# ---------------------------------------------------------------------------
# Loader errors never carry the absolute host path
# ---------------------------------------------------------------------------


def test_missing_role_file_is_named_relative_to_the_root(
    root: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    (root / "vip-support" / "policy.md").unlink()

    message = _error(
        FolderLocator(path=root / "vip-support", root=root), _roots(tmp_path)
    )

    assert "vip-support/policy.md" in message
    _assert_no_host_path(message, tmp_path)


@pytest.mark.parametrize(
    ("extends", "expected"),
    [
        ("../../outside/stolen", "escapes the importer root"),
        ("../missing", "not an existing folder"),
    ],
)
def test_extends_errors_do_not_name_the_root_path(
    root: pathlib.Path,
    outside: pathlib.Path,
    tmp_path: pathlib.Path,
    extends: str,
    expected: str,
) -> None:
    child = _agent(root, "child", extends=extends)

    message = _error(FolderLocator(path=child, root=root), _roots(tmp_path))

    assert expected in message
    assert "agent folder 'child'" in message
    _assert_no_host_path(message, tmp_path)


def test_cycle_error_names_folders_relative_to_the_root(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "agents"
    _agent(root, "a", extends="../b")
    _agent(root, "b", extends="../a")

    message = _error(FolderLocator(path=root / "a", root=root), _roots(tmp_path))

    assert "cycle" in message
    assert "folder:a -> folder:b -> folder:a" in message
    _assert_no_host_path(message, tmp_path)


def test_parent_load_failure_names_both_folders_relative_to_the_root(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "agents"
    (_agent(root, "base") / "role.md").unlink()
    _agent(root, "child", extends="../base")

    message = _error(FolderLocator(path=root / "child", root=root), _roots(tmp_path))

    assert "'folder:child' extends 'folder:base'" in message
    assert "base/role.md" in message
    _assert_no_host_path(message, tmp_path)


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        (
            {
                "manifest_extra": (
                    "command_tools:\n  - name: t\n    argv: [no-such-binary-xyz]\n"
                    "    tier: T2\n    permission: run:t\n"
                )
            },
            "vip-support/manifest.md",
        ),
        ({"policy_extra": "untrusted_input: 'yes'\n"}, "vip-support/policy.md"),
        ({"role_body": "\n\n### design notes\n\nwhy"}, "vip-support/role.md"),
    ],
)
def test_file_content_errors_name_the_file_relative_to_the_root(
    tmp_path: pathlib.Path, kwargs: dict[str, str], expected: str
) -> None:
    root = tmp_path / "agents"
    folder = _agent(root, "vip-support", **kwargs)

    message = _error(FolderLocator(path=folder, root=root), _roots(tmp_path))

    assert expected in message
    _assert_no_host_path(message, tmp_path)


def test_missing_platform_role_is_named_relative_to_the_platform_root(
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "platform" / "roles").mkdir(parents=True)

    message = _error("no-such-role", _roots(tmp_path))

    assert "roles/no-such-role" in message
    _assert_no_host_path(message, tmp_path)
