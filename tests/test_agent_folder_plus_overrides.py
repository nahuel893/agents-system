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


# ---------------------------------------------------------------------------
# `extends=` override (PR #85 review). `extends` is an `Agent` field, so
# `from_folder` accepts it -- and it must then REPLACE the manifest's own
# `extends:` like every other override. It used to be accepted and silently
# dropped, which resolved the agent under the folder's own parent (or none)
# and lost what the caller asked for: that parent's `untrusted_input: true`
# (the R4 T3 barrier) and its tighter execution limits.
# ---------------------------------------------------------------------------

PLATFORM = RootConfig()


def _write_bare_folder(
    base: pathlib.Path,
    name: str,
    *,
    extends: str | None = None,
    permissions: str = "[]",
) -> pathlib.Path:
    """A folder agent that declares no policy of its own, so whatever it
    resolves to comes from its parent."""
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\nBody.\n', encoding="utf-8"
    )
    extends_line = f"extends: {extends}\n" if extends else ""
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\n{extends_line}tools: []\nskills: []\n'
        f"context: {{}}\npermissions: {permissions}\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\n---\n\nBody.\n', encoding="utf-8"
    )
    return folder


def test_extends_override_puts_a_parentless_folder_under_the_requested_role(
    tmp_path: pathlib.Path,
) -> None:
    """The review's repro: `wa-bot` has no `extends:` and T3 permissions.
    Asking for `sales-agent` as its parent brings `untrusted_input: true`,
    so the T3 grants are refused (R4) -- exactly what the inline
    `Agent(name="wa-bot", extends="sales-agent", ...)` already does."""
    from agents_system.agent.spec import Agent
    from agents_system.harness.factory import build_runtime
    from agents_system.harness.injector import ToolRegistry
    from agents_system.permissions import UntrustedInputGrantError

    bot = _write_bare_folder(
        tmp_path / "agents", "wa-bot", permissions="[read:files, exec:command]"
    )
    agent = Agent.from_folder(bot, extends="sales-agent")

    with pytest.raises(UntrustedInputGrantError):
        build_runtime(
            agent._to_locator(), ToolRegistry(), ["read:files", "exec:command"]
        )


def test_extends_override_replaces_the_manifests_own_extends(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_bare_folder(tmp_path / "agents", "reporter", extends="data-agent")
    agent = Agent.from_folder(folder, extends="sales-agent")

    definition = resolve(agent._to_locator(), roots=PLATFORM)

    sales = resolve("sales-agent", roots=PLATFORM)
    data_only_tools = set(resolve("data-agent", roots=PLATFORM).tools) - set(
        sales.tools
    )
    assert data_only_tools, "the two parents must differ for this test to mean anything"
    assert definition.tools == sales.tools
    assert definition.autonomy == sales.autonomy
    assert definition.untrusted_input is True


def test_extends_override_inherits_the_requested_parents_execution_limits(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    plain = _write_bare_folder(tmp_path / "agents", "plain")
    agent = Agent.from_folder(plain, extends="operator-agent")

    definition = resolve(agent._to_locator(), roots=PLATFORM)

    operator_limits = resolve("operator-agent", roots=PLATFORM).execution_limits
    assert operator_limits, "operator-agent tightens the platform defaults"
    assert definition.execution_limits == operator_limits


def test_extends_override_accepts_another_agent(tmp_path: pathlib.Path) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_bare_folder(tmp_path / "agents", "bot", extends="agent")
    parent = Agent(name="sales-plus", extends="sales-agent", tools=["extra_tool"])
    agent = Agent.from_folder(folder, extends=parent)

    definition = resolve(agent._to_locator(), roots=PLATFORM)

    assert "extra_tool" in definition.tools
    assert definition.untrusted_input is True
    assert definition.role_name == "bot"


def test_extends_override_path_is_relative_to_the_agents_own_folder(
    tmp_path: pathlib.Path,
) -> None:
    """A path value is placed exactly like a manifest's `extends:`: relative
    to the agent's folder, inside its importer root."""
    from agents_system.agent.spec import Agent

    agents = tmp_path / "agents"
    _write_bare_folder(agents, "mid", extends="operator-agent")
    leaf = _write_bare_folder(agents, "leaf")
    agent = Agent.from_folder(leaf, extends="../mid")

    definition = resolve(agent._to_locator(), roots=PLATFORM)

    operator_limits = resolve("operator-agent", roots=PLATFORM).execution_limits
    assert definition.execution_limits == operator_limits


def test_the_safety_ceiling_is_measured_against_the_override_parent(
    tmp_path: pathlib.Path,
) -> None:
    """15 tool calls is under the platform default (20), which is all a
    parentless folder is held to -- but above operator-agent's 10."""
    from agents_system.agent.spec import Agent

    plain = _write_bare_folder(tmp_path / "agents", "plain")
    agent = Agent.from_folder(
        plain, extends="operator-agent", execution_limits={"max_tool_calls": 15}
    )

    with pytest.raises(DefinitionError, match="max_tool_calls"):
        resolve(agent._to_locator(), roots=PLATFORM)


@pytest.mark.parametrize("value", ["/etc", "../..", ".", "", None, 42])
def test_an_unplaceable_extends_override_fails_loud(
    tmp_path: pathlib.Path, value: object
) -> None:
    """Every value the manifest's `extends:` rejects is rejected here too,
    and the message says it came from the Python override."""
    from agents_system.agent.spec import Agent

    folder = _write_bare_folder(tmp_path / "agents", "bot", extends="agent")
    agent = Agent.from_folder(folder, extends=value)

    with pytest.raises(DefinitionError, match="extends= override"):
        resolve(agent._to_locator(), roots=PLATFORM)


def test_an_extends_override_back_to_the_same_folder_is_a_cycle(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_bare_folder(tmp_path / "agents", "bot", extends="agent")
    agent = Agent.from_folder(folder, extends=Agent.from_folder(folder))

    with pytest.raises(DefinitionError, match="cycle"):
        resolve(agent._to_locator(), roots=PLATFORM)
