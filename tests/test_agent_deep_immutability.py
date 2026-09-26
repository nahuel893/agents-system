"""`Agent` is immutable all the way down, not just against reassignment.

PR #85 review, finding 2 (MEDIUM). `frozen=True` only blocks
`agent.tools = ...`; a list or dict the caller passed in and still holds
could be mutated in place afterwards, and every later `_to_locator()` /
`resolve()` -- including one reached through another `Agent`'s `extends=`
-- read the mutated container. `Agent` now copies every container field
into an immutable shape at construction (tuples, `MappingProxyType`, nested
values included), and `_to_locator()` hands the loader fresh plain
lists/dicts, the shapes it parses from YAML.
"""

from __future__ import annotations

import pathlib
import types
from typing import Any

import pytest

from agents_system.harness.loader import RootConfig, resolve

ROOTS = RootConfig()


def _write_folder(base: pathlib.Path, name: str) -> pathlib.Path:
    folder = base / name
    folder.mkdir(parents=True)
    (folder / "role.md").write_text(
        f'---\nname: {name}\nversion: "1.0"\n---\n\n# Role: {name}\n\nBody.\n',
        encoding="utf-8",
    )
    (folder / "manifest.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nextends: agent\ntools: []\n'
        "skills: []\ncontext: {}\npermissions: []\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (folder / "policy.md").write_text(
        f'---\nrole: {name}\nversion: "1.0"\nautonomy: supervised\n'
        "execution_limits: null\n---\n\nBody.\n",
        encoding="utf-8",
    )
    return folder


def test_mutating_the_callers_tools_list_does_not_change_resolution() -> None:
    from agents_system.agent.spec import Agent

    tools = ["catalog_search"]
    agent = Agent(name="x", extends="agent", tools=tools)  # type: ignore[arg-type]
    before = resolve(agent._to_locator(), roots=ROOTS)

    tools.append("order_writer")

    after = resolve(agent._to_locator(), roots=ROOTS)
    assert after.tools == before.tools
    assert "order_writer" not in after.tools


def test_mutating_the_callers_context_dict_nested_included_changes_nothing() -> None:
    from agents_system.agent.spec import Agent

    nested: dict[str, Any] = {"tool_derived": ["catalog_search"]}
    context: dict[str, Any] = {"session": True, "extra": nested}
    agent = Agent(name="x", extends="agent", context=context)
    before = resolve(agent._to_locator(), roots=ROOTS)

    context["untrusted_key"] = "injected"
    nested["tool_derived"].append("order_writer")
    nested["new"] = True

    after = resolve(agent._to_locator(), roots=ROOTS)
    assert after.context == before.context
    assert "untrusted_key" not in after.context


def test_mutating_a_parent_agents_permissions_after_use_as_extends_changes_nothing() -> (
    None
):
    """The reviewer's probe 5: the child was already resolved once, then the
    caller mutated the list it had handed to the PARENT."""
    from agents_system.agent.spec import Agent

    permissions = ["read:catalog"]
    parent = Agent(
        name="base-agent",
        extends="agent",
        permissions=permissions,  # type: ignore[arg-type]
    )
    child = Agent(name="child-agent", extends=parent, tools=("vip_perk",))
    before = resolve(child._to_locator(), roots=ROOTS)

    permissions.append("write:orders")

    after = resolve(child._to_locator(), roots=ROOTS)
    assert after.permissions == before.permissions
    assert "write:orders" not in after.permissions


def test_mutating_the_callers_execution_limits_changes_nothing() -> None:
    from agents_system.agent.spec import Agent

    limits: dict[str, Any] = {"max_tool_calls": 5}
    agent = Agent(name="x", extends="agent", execution_limits=limits)

    limits["max_tool_calls"] = 999999

    definition = resolve(agent._to_locator(), roots=ROOTS)
    assert definition.execution_limits is not None
    assert definition.execution_limits["max_tool_calls"] == 5


def test_mutating_a_from_folder_override_changes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    from agents_system.agent.spec import Agent

    folder = _write_folder(tmp_path, "folder-bot")
    permissions = ["read:catalog"]
    agent = Agent.from_folder(folder, permissions=permissions)

    permissions.append("write:orders")

    definition = resolve(agent._to_locator(), roots=ROOTS)
    assert "read:catalog" in definition.permissions
    assert "write:orders" not in definition.permissions


def test_container_fields_are_frozen_shapes() -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="x",
        tools=["a"],  # type: ignore[arg-type]
        permissions=["read:catalog"],  # type: ignore[arg-type]
        skills=["tone"],  # type: ignore[arg-type]
        skill_contents={"tone": "Be brief."},
        context={"extra": {"list": [1, 2]}},
        escalation_rules={"conditions": ["angry customer"]},
        execution_limits={"max_tool_calls": 5},
    )

    assert agent.tools == ("a",)
    assert agent.permissions == ("read:catalog",)
    assert agent.skills == ("tone",)
    for mapping in (
        agent.skill_contents,
        agent.context,
        agent.escalation_rules,
        agent.execution_limits,
    ):
        assert isinstance(mapping, types.MappingProxyType)
    assert isinstance(agent.context["extra"], types.MappingProxyType)
    assert agent.context["extra"]["list"] == (1, 2)
    assert agent.escalation_rules["conditions"] == ("angry customer",)
    with pytest.raises(TypeError):
        agent.context["new"] = 1  # type: ignore[index]


def test_frozen_shapes_still_resolve_to_the_loaders_plain_shapes(
    tmp_path: pathlib.Path,
) -> None:
    """The loader parses YAML lists/dicts; a tuple where it expects a list
    would be silently dropped (e.g. a permissions override) or mangled
    (`_as_str_list` of a tuple). `_to_locator()` thaws before handing over."""
    from agents_system.agent.spec import Agent

    inline = resolve(
        Agent(
            name="x",
            extends="agent",
            escalation_rules={"conditions": ["angry customer"]},
        )._to_locator(),
        roots=ROOTS,
    )
    assert inline.escalation_rules["conditions"] == ["angry customer"]

    folder = _write_folder(tmp_path, "folder-bot")
    from_folder = resolve(
        Agent.from_folder(
            folder,
            permissions=("read:catalog",),
            execution_limits={"max_tool_calls": 5},
        )._to_locator(),
        roots=ROOTS,
    )
    assert "read:catalog" in from_folder.permissions
    assert from_folder.execution_limits is not None
    assert from_folder.execution_limits["max_tool_calls"] == 5
