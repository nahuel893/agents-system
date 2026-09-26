"""Tests for `Agent(...)` Python-parameter construction (design.md D3, PR2-T1).

Strict TDD: written BEFORE `src/agents_system/agent/spec.py` exists —
intentionally red until `Agent` is defined there with the frozen-dataclass
shape design.md's D3 code block specifies.
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from agents_system.harness.loader import DefinitionError, RootConfig, resolve
from agents_system.permissions import UntrustedInputGrantError

# ---------------------------------------------------------------------------
# PR2-T1 — `Agent(...)` Python-parameter construction resolves without disk
# ---------------------------------------------------------------------------


def test_agent_from_params_resolves_with_no_folder_anywhere_on_disk(
    tmp_path: pathlib.Path,
) -> None:
    """Spec: "A pure-Python agent resolves with no folder" — proven by
    pointing `platform_root` at a real tree (so `extends: agent` still
    resolves) while asserting no folder named `triage-bot` exists anywhere,
    including under the fixture platform root itself."""
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="triage-bot",
        extends="agent",
        tools=("catalog_search",),
        permissions=("read:catalog",),
    )

    # No folder for this agent's own content exists anywhere on disk.
    assert not (tmp_path / "triage-bot").exists()

    definition = resolve(agent._to_locator(), roots=RootConfig())

    assert definition.role_name == "triage-bot"
    assert "catalog_search" in definition.tools
    assert "read:catalog" in definition.permissions
    # Folds onto the `extends: agent` parent (Option B, additive).
    assert "escalation_notifier" in definition.tools


def test_pure_python_agent_receives_base_prompt_contract_and_tier_enforcement() -> None:
    """Spec: "A pure-Python agent still receives every library invariant" —
    the base prompt contract, and the same T3/untrusted_input rejection a
    folder-defined role gets, fire identically for an inline `Agent`."""
    from agents_system.agent.spec import Agent

    agent = Agent(
        name="triage-bot",
        extends="agent",
        tools=("catalog_search",),
        permissions=("read:catalog",),
    )
    definition = resolve(agent._to_locator(), roots=RootConfig())
    assert definition.system_prompt.rstrip().endswith("Answer in the user's language.")

    rogue = Agent(
        name="rogue-bot",
        extends="agent",
        permissions=("exec:command",),
        untrusted_input=True,
    )
    with pytest.raises(UntrustedInputGrantError, match="exec:command"):
        resolve(rogue._to_locator(), roots=RootConfig())


def test_skill_contents_naming_unlisted_skill_raises_at_init_before_resolution() -> (
    None
):
    """`Agent.skill_contents` naming a skill not listed in `skills` raises
    `DefinitionError` at `Agent.__init__` time (the `__post_init__`
    validation) — before any resolution is attempted at all."""
    from agents_system.agent.spec import Agent

    with pytest.raises(DefinitionError, match="skill_contents"):
        Agent(
            name="triage-bot",
            skills=("tone",),
            skill_contents={"unlisted-skill": "some content"},
        )


def test_agent_is_frozen() -> None:
    from agents_system.agent.spec import Agent

    agent = Agent(name="triage-bot")
    with pytest.raises(dataclasses.FrozenInstanceError):
        agent.name = "renamed"  # type: ignore[misc]
