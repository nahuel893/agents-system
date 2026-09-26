"""Tests for `extends: str | Agent` — the eager, Agent-to-Agent half
(design.md D3, PR2-T3).

Test-only per the task: `Agent`'s `extends: str | Agent` field and
`_to_locator()`'s branch on `isinstance(self.extends, Agent)` already ship
from PR2-T1/PR2-T2 — this file proves that eager/lazy split holds, rather
than adding new production code.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from agents_system.harness.loader import FolderLocator, InlineLocator


def test_agent_extends_agent_resolves_parent_eagerly_with_no_disk_io(
    tmp_path: pathlib.Path,
) -> None:
    """`extends=<another Agent>` resolves to that parent's own locator at
    `_to_locator()` call time via pure Python object identity — no I/O, even
    when the parent is folder-backed and that folder does not exist anywhere
    on disk (proven by never raising despite the nonexistent path)."""
    from agents_system.agent.spec import Agent

    nonexistent_folder = tmp_path / "does-not-exist-anywhere"
    parent = Agent.from_folder(nonexistent_folder)
    child = Agent(name="vip-support", extends=parent, tools=("vip_perk",))

    locator = child._to_locator()  # must not raise — no disk touched

    assert isinstance(locator, InlineLocator)
    assert isinstance(locator.parent, FolderLocator)
    assert locator.parent.path == nonexistent_folder


def test_frozen_parent_agent_cannot_be_mutated_after_use_as_extends() -> None:
    """The immutability argument design.md D3 makes for why an `Agent`-to-
    `Agent` extends chain is cycle-free by construction: an already-built
    `Agent` used as another's `extends=` cannot be mutated afterward -- not
    by reassigning a field, not by mutating a field's container in place,
    and not through the list/dict the caller originally passed in."""
    from agents_system.agent.spec import Agent
    from agents_system.harness.loader import RootConfig, resolve

    tools = ["catalog_search"]
    context = {"extra": {"flags": ["a"]}}
    parent = Agent(
        name="base-agent",
        extends="agent",
        tools=tools,  # type: ignore[arg-type]
        context=context,
    )
    child = Agent(name="child-agent", extends=parent)
    before = resolve(child._to_locator(), roots=RootConfig())

    with pytest.raises(dataclasses.FrozenInstanceError):
        parent.name = "renamed"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        parent.tools.append("order_writer")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        parent.context["extra"] = {}  # type: ignore[index]

    tools.append("order_writer")
    context["extra"]["flags"].append("b")

    after = resolve(child._to_locator(), roots=RootConfig())
    assert after.tools == before.tools
    assert after.context == before.context
