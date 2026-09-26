"""Tests for `Agent.from_folder` (design.md D3, PR2-T2).

Strict TDD: written BEFORE `Agent.from_folder`/`_AGENT_OVERRIDABLE_FIELDS`
exist — intentionally red until PR2-T2's GREEN change lands.
"""

from __future__ import annotations

import pathlib

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
    tools: str = "[tool_x]",
) -> pathlib.Path:
    """Write a minimal, well-formed importer-agent folder (mirrors
    `test_locator_folder.py`'s helper, matching the same three-file
    contract)."""
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n# Role: {name}\n\nProse body.\n',
        encoding="utf-8",
    )
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\ntools: {tools}\nskills: []\n'
        "context: {}\npermissions:\n  - read:x\n---\n\nManifest body.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nautonomy: supervised\n'
        "execution_limits: null\n---\n\nPolicy body.\n",
        encoding="utf-8",
    )
    return folder


def _roots(tmp_path: pathlib.Path) -> RootConfig:
    return RootConfig(
        platform_root=tmp_path / "unused-platform",
        deployments_root=tmp_path / "unused-deployments",
    )


def test_from_folder_output_shape_matches_a_direct_folder_locator(
    tmp_path: pathlib.Path,
) -> None:
    """`Agent.from_folder(path)` with no overrides produces the identical
    `AgentDefinition` a plain `FolderLocator(path, root)` produces for the
    same folder — proving `from_folder` is not a different, parallel code
    path with its own subtly different shape."""
    from agents_system.agent.spec import Agent

    folder = _write_folder(tmp_path, "support-bot")
    roots = _roots(tmp_path)

    via_agent = resolve(Agent.from_folder(folder)._to_locator(), roots=roots)
    via_locator = resolve(FolderLocator(path=folder, root=folder.parent), roots=roots)

    assert isinstance(via_agent, AgentDefinition)
    assert via_agent == via_locator


def test_from_folder_performs_zero_filesystem_access_at_construction_time(
    tmp_path: pathlib.Path,
) -> None:
    """`Agent.from_folder(path)` reads nothing at call time — proven by
    calling it against a path that does not exist yet, then creating the
    folder afterward, and only THEN resolving it."""
    from agents_system.agent.spec import Agent

    not_yet_created = tmp_path / "support-bot"
    assert not not_yet_created.exists()

    agent = Agent.from_folder(not_yet_created)  # must not raise

    _write_folder(tmp_path, "support-bot")
    definition = resolve(agent._to_locator(), roots=_roots(tmp_path))

    assert definition.role_name == "support-bot"
