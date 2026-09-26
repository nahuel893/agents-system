"""Tests for `_load_skills`'s 4-source precedence (design.md D4, PR3-T2/T3):
inline content > an importer's own folder > deployment > `FactoryError`.

Strict TDD: written BEFORE `_load_skills` is rewritten to this precedence —
intentionally red until PR3-T2/T3's GREEN change lands.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from agents_system.harness.factory import FactoryError, _load_skills
from agents_system.harness.loader import (
    AgentDefinition,
    FolderLocator,
    RootConfig,
    resolve,
)


def _write_folder(
    base: pathlib.Path,
    name: str,
    *,
    skills: list[str] | None = None,
) -> pathlib.Path:
    """Write a minimal, well-formed importer-agent folder with a `skills/`
    subdirectory holding one `.md` file per declared skill name (mirrors
    `test_agent_from_folder.py`'s helper)."""
    folder = base / name
    folder.mkdir(parents=True)
    skill_names = skills or []
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n# Role: {name}\n\nProse body.\n',
        encoding="utf-8",
    )
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\ntools: []\nskills: {skill_names}\n'
        "context: {}\npermissions:\n  - read:x\n---\n\nManifest body.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nautonomy: supervised\n'
        "execution_limits: null\n---\n\nPolicy body.\n",
        encoding="utf-8",
    )
    skills_dir = folder / "skills"
    skills_dir.mkdir()
    for skill_name in skill_names:
        (skills_dir / f"{skill_name}.md").write_text(
            f"{skill_name} content from the importer's own folder.\n",
            encoding="utf-8",
        )
    return folder


def _roots(tmp_path: pathlib.Path) -> RootConfig:
    return RootConfig(
        platform_root=tmp_path / "unused-platform",
        deployments_root=tmp_path / "deployments",
    )


# ---------------------------------------------------------------------------
# PR3-T2 — an importer agent's own skills/ resolves with no deployment
# ---------------------------------------------------------------------------


def test_importer_folder_own_skills_resolves_with_no_deployment(
    tmp_path: pathlib.Path,
) -> None:
    """Spec: "An importer agent's own skills/ resolves with no deployment" —
    the deployment-only `FactoryError` ("declares skills but no client
    deployment") must NOT be raised for this agent."""
    folder = _write_folder(tmp_path, "support-bot", skills=["pricing-nuance"])
    roots = _roots(tmp_path)
    definition = resolve(FolderLocator(path=folder, root=folder.parent), roots=roots)

    loaded = _load_skills(definition, None, roots)

    assert len(loaded) == 1
    assert loaded[0].name == "pricing-nuance"
    assert loaded[0].content == "pricing-nuance content from the importer's own folder."


def test_missing_importer_folder_skill_file_raises_naming_skill_and_path(
    tmp_path: pathlib.Path,
) -> None:
    """Spec: "A missing importer-folder skill file fails the same way a
    missing deployment skill file does" — the raised `FactoryError` names
    both the missing skill and its expected path."""
    folder = _write_folder(tmp_path, "support-bot", skills=[])
    # Declare a skill the manifest never wrote a file for.
    manifest = folder / "manifest.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(
            "skills: []", "skills: [missing-skill]"
        ),
        encoding="utf-8",
    )
    roots = _roots(tmp_path)
    definition = resolve(FolderLocator(path=folder, root=folder.parent), roots=roots)
    expected_path = folder / "skills" / "missing-skill.md"

    with pytest.raises(FactoryError) as excinfo:
        _load_skills(definition, None, roots)

    message = str(excinfo.value)
    assert "missing-skill" in message
    assert str(expected_path) in message


# ---------------------------------------------------------------------------
# PR3-T2 — inline Python-supplied skill content
# ---------------------------------------------------------------------------


def test_inline_skill_content_used_verbatim_with_no_file_read(
    tmp_path: pathlib.Path,
) -> None:
    """Spec: "Inline skill content is used verbatim" — `Agent(skill_contents=
    {...})`'s content is exactly the inline-supplied string, with no file
    read at all (`roots` points nowhere real)."""
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="triage-bot",
        extends="agent",
        skills=("tone",),
        skill_contents={"tone": "Always answer in a formal register."},
    )
    definition = resolve(agent._to_locator(), roots=RootConfig())

    loaded = _load_skills(definition, None, _roots(tmp_path))

    assert len(loaded) == 1
    assert loaded[0].name == "tone"
    assert loaded[0].content == "Always answer in a formal register."


def test_inline_skill_content_overrides_a_same_named_folder_skill(
    tmp_path: pathlib.Path,
) -> None:
    """Spec: "Inline skill content overrides a same-named folder skill" —
    `Agent.from_folder(path, skill_contents={"tone": "override text"})`
    resolves `tone` to `"override text"`, not the folder file's content."""
    from agents_system.agent.spec import Agent

    folder = _write_folder(tmp_path, "support-bot", skills=["tone"])
    roots = _roots(tmp_path)

    agent = Agent.from_folder(folder, skill_contents={"tone": "override text"})
    definition = resolve(agent._to_locator(), roots=roots)

    loaded = _load_skills(definition, None, roots)

    assert len(loaded) == 1
    assert loaded[0].content == "override text"


# ---------------------------------------------------------------------------
# PR3-T2 — predefined-role deployment-only skills remain unchanged
# ---------------------------------------------------------------------------


def test_predefined_role_with_no_deployment_and_declared_skills_still_fails(
    tmp_path: pathlib.Path,
) -> None:
    """Spec: "A predefined role with no deployment and declared skills still
    fails the same way" — unchanged regression."""
    definition = AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="",
        tools=(),
        skills=("ghost_skill",),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )

    with pytest.raises(FactoryError):
        _load_skills(definition, None, _roots(tmp_path))


def test_predefined_role_own_platform_folder_never_treated_as_skills_source(
    tmp_path: pathlib.Path,
) -> None:
    """Spec: "A predefined role's own platform-role folder is never treated
    as a skills source" — even when `platform_root/roles/<name>/skills/`
    happens to exist, skills load only from `deployments/{client}/{role}/
    skills/`, never from the platform-role folder itself."""
    platform_root = tmp_path / "platform"
    role_folder = platform_root / "roles" / "simple-role"
    role_folder.mkdir(parents=True)
    (role_folder / "role.md").write_text(
        '---\nname: simple-role\nversion: "1.0"\n---\n\n# Role\n\nBody.\n',
        encoding="utf-8",
    )
    (role_folder / "manifest.md").write_text(
        '---\nrole: simple-role\nversion: "1.0"\ntools: []\n'
        "skills: [tone]\ncontext: {}\npermissions: []\n---\n\nManifest.\n",
        encoding="utf-8",
    )
    (role_folder / "policy.md").write_text(
        '---\nrole: simple-role\nversion: "1.0"\nautonomy: supervised\n'
        "execution_limits: null\n---\n\nPolicy.\n",
        encoding="utf-8",
    )
    # The platform role's OWN folder happens to contain a skills/ dir.
    own_skills = role_folder / "skills"
    own_skills.mkdir()
    (own_skills / "tone.md").write_text("Platform-owned content.\n", encoding="utf-8")

    roots = RootConfig(
        platform_root=platform_root, deployments_root=tmp_path / "deployments"
    )
    definition = resolve("simple-role", roots=roots)

    assert definition.skills_folder is None
    with pytest.raises(FactoryError):
        _load_skills(definition, None, roots)


# ---------------------------------------------------------------------------
# PR3-T3 — threat matrix: importer skills/ symlink containment
# ---------------------------------------------------------------------------


def test_skill_symlink_does_not_escape_folder(tmp_path: pathlib.Path) -> None:
    """Threat matrix: an importer folder's `skills/` directory contains a
    symlink pointing outside that folder, named to match a declared skill.
    `_load_skills` must either read only the file at the expected in-folder
    path (no escape) or raise `FactoryError` — never silently read content
    from outside the importer's own root."""
    secret = tmp_path / "secret.md"
    secret.write_text("SECRET outside the importer folder.\n", encoding="utf-8")

    folder = _write_folder(tmp_path, "support-bot", skills=[])
    manifest = folder / "manifest.md"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("skills: []", "skills: [tone]"),
        encoding="utf-8",
    )
    skills_dir = folder / "skills"
    os.symlink(secret, skills_dir / "tone.md")

    roots = _roots(tmp_path)
    definition = resolve(FolderLocator(path=folder, root=folder.parent), roots=roots)

    try:
        loaded = _load_skills(definition, None, roots)
    except FactoryError:
        return  # Failing loud is an accepted outcome.
    assert loaded[0].content != "SECRET outside the importer folder."
