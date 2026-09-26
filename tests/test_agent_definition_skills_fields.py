"""Tests for `AgentDefinition`/`RawDefinition` gaining `skills_folder`/
`inline_skills` (design.md D4, PR3-T1).

Strict TDD: written BEFORE the two fields exist on `AgentDefinition` —
intentionally red until PR3-T1's GREEN change lands.
"""

from __future__ import annotations

import dataclasses
import pathlib

from agents_system.harness.loader import (
    AgentDefinition,
    FolderLocator,
    RawDefinition,
    RootConfig,
    resolve,
)


def _write_folder(
    base: pathlib.Path,
    name: str,
    *,
    skills: list[str] | None = None,
) -> pathlib.Path:
    """Write a minimal, well-formed importer-agent folder: role.md/manifest.md/
    policy.md (mirrors `test_locator_folder.py`'s helper), plus a `skills/`
    subdirectory with one `.md` file per declared skill name."""
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
    if skill_names:
        skills_dir = folder / "skills"
        skills_dir.mkdir()
        for skill_name in skill_names:
            (skills_dir / f"{skill_name}.md").write_text(
                f"{skill_name} content.\n", encoding="utf-8"
            )
    return folder


def _roots(tmp_path: pathlib.Path) -> RootConfig:
    return RootConfig(
        platform_root=tmp_path / "unused-platform",
        deployments_root=tmp_path / "unused-deployments",
    )


# ---------------------------------------------------------------------------
# PR3-T1 — the carrier fields exist, additive and defaulted
# ---------------------------------------------------------------------------


def test_new_fields_exist_on_both_dataclasses_and_default_to_none_and_empty() -> None:
    """`AgentDefinition`/`RawDefinition` both declare the two new fields, and
    neither one is required — an existing call site that never sets either
    keeps working unchanged (design.md D4)."""
    raw_field_names = {f.name for f in dataclasses.fields(RawDefinition)}
    def_field_names = {f.name for f in dataclasses.fields(AgentDefinition)}
    assert {"skills_folder", "inline_skills"} <= raw_field_names
    assert {"skills_folder", "inline_skills"} <= def_field_names

    raw = RawDefinition(
        role_name="r",
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
    )
    assert raw.skills_folder is None
    assert raw.inline_skills == {}


def test_platform_role_definition_has_no_skills_folder_or_inline_skills(
    tmp_path: pathlib.Path,
) -> None:
    """A platform-role `AgentDefinition` gets `skills_folder=None,
    inline_skills={}` — the explicit backward-compatibility regression this
    task exists to prove: every existing field a platform role resolved to
    before this change is completely unaffected."""
    from agents_system.agent.spec import Agent

    agent = Agent(name="triage-bot", extends="agent")
    definition = resolve(agent._to_locator(), roots=RootConfig())

    parent_definition = resolve("agent", roots=RootConfig())
    assert parent_definition.skills_folder is None
    assert parent_definition.inline_skills == {}
    # The agent's own chain composes onto `agent`, which is itself a
    # platform role with no folder/inline skills source of its own.
    assert isinstance(definition, AgentDefinition)


def test_folder_locator_definition_carries_its_own_skills_folder(
    tmp_path: pathlib.Path,
) -> None:
    """A `FolderLocator`-sourced `AgentDefinition` has `skills_folder` set to
    that importer folder's own `skills/` subdirectory — the location D4's
    `_load_skills` formula (`skills_folder / f"{name}.md"`) and the spec's
    own scenario (`skills/pricing-nuance.md`) both name."""
    folder = _write_folder(tmp_path, "support-bot", skills=["pricing-nuance"])
    roots = _roots(tmp_path)

    definition = resolve(FolderLocator(path=folder, root=folder.parent), roots=roots)

    assert definition.skills_folder == folder / "skills"
    assert definition.inline_skills == {}


def test_inline_locator_definition_carries_inline_skills_from_agent(
    tmp_path: pathlib.Path,
) -> None:
    """An `InlineLocator`-sourced `AgentDefinition` (via
    `Agent(skill_contents={...})`) has `inline_skills` populated from the
    `Agent`'s own `skill_contents`."""
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="triage-bot",
        extends="agent",
        skills=("tone",),
        skill_contents={"tone": "Always answer in a formal register."},
    )
    definition = resolve(agent._to_locator(), roots=RootConfig())

    assert definition.inline_skills == {"tone": "Always answer in a formal register."}
    assert definition.skills_folder is None
