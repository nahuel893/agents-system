"""Tests for folder + Python-parameter override composition (design.md D3,
PR2-T2) — "Folder content and Python parameters compose, with parameters as
the override layer".

Strict TDD: written BEFORE `FolderLocator.overrides` is consumed by
`_load_role_files` — intentionally red until PR2-T2's GREEN change lands.
"""

from __future__ import annotations

import pathlib

import pytest

from agents_system.harness.loader import DefinitionError, RootConfig, resolve


def _write_folder(base: pathlib.Path, name: str) -> pathlib.Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n# Role: {name}\n\nProse body.\n',
        encoding="utf-8",
    )
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\ntools: [catalog_search]\nskills: []\n'
        "context: {}\npermissions:\n  - read:catalog\n---\n\nManifest body.\n",
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


def test_explicit_tools_override_replaces_the_folders_declared_tools(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(tmp_path, "sales-bot")

    agent = Agent.from_folder(folder, tools=["catalog_search", "order_writer"])
    definition = resolve(agent._to_locator(), roots=_roots(tmp_path))

    assert definition.tools == ("catalog_search", "order_writer")


def test_a_field_not_passed_as_override_keeps_the_folders_declared_value(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(tmp_path, "sales-bot")

    agent = Agent.from_folder(folder, tools=["catalog_search", "order_writer"])
    definition = resolve(agent._to_locator(), roots=_roots(tmp_path))

    # `permissions` was never passed as an override — the folder's own
    # declared value survives untouched.
    assert definition.permissions == ("read:catalog",)


def test_unknown_override_field_raises_naming_it(tmp_path: pathlib.Path) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(tmp_path, "sales-bot")

    with pytest.raises(DefinitionError, match="bogus_field"):
        Agent.from_folder(folder, bogus_field="whatever")
