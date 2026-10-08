"""ADR-010 part 2 (#222) -- the reference agents in ``examples/agents/``.

The eight concrete predefined roles have copies under ``examples/agents/``.
They are examples to copy, not supported API, and they resolve through
exactly the path an adopter's folder takes: ``Agent.from_folder`` and the
importer ceiling (``_apply_importer_ceiling``), with no special case.

This file walks ``examples/agents/`` on disk, so a reference agent added
later is checked without anyone extending a tuple. Five of the copies were
fitted to the importer ceiling; ``_POLICY_CHANGES`` pins each change against
the predefined role it was copied from, and every other field must match.
"""

from __future__ import annotations

import pathlib
import warnings
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
import yaml
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage

from agents_system.agent.graph import AgentRuntime
from agents_system.agent.spec import Agent
from agents_system.harness import loader
from agents_system.harness.factory import build_runtime
from agents_system.harness.loader import (
    AgentDefinition,
    PredefinedRoleDeprecationWarning,
    resolve,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES_ROOT = REPO_ROOT / "examples" / "agents"

#: The eight concrete predefined roles ADR-010 turns into reference agents.
REFERENCE_AGENTS = (
    "accountant-agent",
    "data-agent",
    "developer-agent",
    "operator-agent",
    "orchestrator",
    "sales-agent",
    "summary-agent",
    "support-agent",
)

#: The only parent each reference agent may name: the generic `agent`, or a
#: sibling folder. Never a concrete predefined role by name.
EXPECTED_EXTENDS = {
    name: "../operator-agent" if name == "developer-agent" else "platform/roles/agent"
    for name in REFERENCE_AGENTS
}


def discover_reference_agents() -> list[str]:
    """Every agent folder under ``examples/agents/``, read from disk. A missing
    root is an empty walk, which the pinned list below then fails."""
    if not EXAMPLES_ROOT.is_dir():
        return []
    return sorted(path.name for path in EXAMPLES_ROOT.iterdir() if path.is_dir())


def _folder_agent(name: str) -> Agent:
    return Agent.from_folder(EXAMPLES_ROOT / name)


@pytest.fixture
def no_deprecated_role() -> Iterator[None]:
    """A reference agent never names a deprecated predefined role, so
    resolving one must not warn."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", PredefinedRoleDeprecationWarning)
        yield


def _predefined(name: str) -> AgentDefinition:
    """The predefined role a reference agent was copied from. Resolving it by
    name is deprecated, which is what this asserts."""
    with pytest.warns(PredefinedRoleDeprecationWarning):
        return resolve(name)


def _base_description(condition: str) -> str:
    return " ".join(
        str(resolve("agent").escalation_rules["descriptions"][condition]).split()
    )


def _description(definition: AgentDefinition, condition: str) -> str:
    return " ".join(str(definition.escalation_rules["descriptions"][condition]).split())


# ---------------------------------------------------------------------------
# The folders on disk
# ---------------------------------------------------------------------------


def test_examples_agents_holds_the_eight_reference_agents() -> None:
    assert discover_reference_agents() == sorted(REFERENCE_AGENTS)
    for name in REFERENCE_AGENTS:
        for filename in ("role.md", "manifest.md", "policy.md"):
            assert (EXAMPLES_ROOT / name / filename).is_file(), f"{name}/{filename}"


@pytest.mark.parametrize("name", discover_reference_agents())
def test_reference_agent_extends_agent_or_a_sibling_never_a_concrete_role(
    name: str,
) -> None:
    text = (EXAMPLES_ROOT / name / "manifest.md").read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---", 2)[1])

    assert frontmatter["extends"] == EXPECTED_EXTENDS[name]


# ---------------------------------------------------------------------------
# Resolution: the importer path, no special case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", discover_reference_agents())
def test_reference_agent_resolves_through_the_importer_ceiling(
    name: str, monkeypatch: pytest.MonkeyPatch, no_deprecated_role: None
) -> None:
    """Every hop that the folder authors is held to its parent's ceiling by
    the same `_apply_importer_ceiling` an adopter's folder goes through."""
    checked: list[tuple[str | None, str]] = []
    real = loader._apply_importer_ceiling

    def spy(parent: Any, child: Any, **kwargs: Any) -> Any:
        checked.append((None if parent is None else parent.role_name, child.role_name))
        return real(parent, child, **kwargs)

    monkeypatch.setattr(loader, "_apply_importer_ceiling", spy)

    definition = resolve(_folder_agent(name))

    assert definition.role_name == name
    if name == "developer-agent":
        assert checked == [
            ("agent", "operator-agent"),
            ("operator-agent", "developer-agent"),
        ]
    else:
        assert checked == [("agent", name)]


@pytest.mark.parametrize("name", discover_reference_agents())
async def test_reference_agent_boots_end_to_end_with_a_fake_model(
    name: str, no_deprecated_role: None
) -> None:
    from conftest import build_test_registry

    agent = _folder_agent(name)
    definition = resolve(agent)
    equipped = build_runtime(agent, build_test_registry(), definition.permissions)

    assert equipped.denied_tools == ()
    assert {tool.name for tool in equipped.tools} == set(definition.tools)

    class ScriptedModel(FakeMessagesListChatModel):
        def bind_tools(  # type: ignore[override]
            self, tools: Sequence[Any], **kwargs: Any
        ) -> ScriptedModel:
            return self

    model = ScriptedModel(
        responses=[AIMessage(content="Hello from the reference agent.")]
    )
    messages = await AgentRuntime(equipped, model=model).run_turn(
        [HumanMessage(content="Hello")], session_id=f"reference-{name}"
    )

    assert messages[-1].content == "Hello from the reference agent."


# ---------------------------------------------------------------------------
# Faithful copies, except the policy changes that fit the importer ceiling
# ---------------------------------------------------------------------------

#: Each change a reference agent makes to the predefined role it copies, as
#: (field, predefined value, reference value). Every other field matches.
_POLICY_CHANGES: dict[str, dict[str, tuple[Any, Any]]] = {
    "data-agent": {"autonomy": ("full", "supervised")},
    "summary-agent": {"autonomy": ("full", "supervised")},
    "developer-agent": {
        "execution_limits": (
            {
                "tool_call_timeout_s": 10,
                "total_execution_timeout_s": 120,
                "max_tool_calls": 10,
            },
            {
                "tool_call_timeout_s": 10,
                "total_execution_timeout_s": 30,
                "max_tool_calls": 10,
            },
        )
    },
}

#: Conditions a reference agent adds over the predefined role it copies.
_ADDED_CONDITIONS = {"sales-agent": {"catalog_match_below_threshold"}}

#: Inherited conditions whose wording the predefined role rewrote and the
#: reference agent leaves to its parent (`_validate_escalation_wording`).
_REWORDED_INHERITED = {
    "data-agent": {"required_tool_missing"},
    "summary-agent": {"required_tool_missing"},
    "orchestrator": {"required_tool_missing"},
    "sales-agent": {"required_tool_missing", "confidence_below_threshold"},
}

_COMPARED_FIELDS = (
    "tools",
    "permissions",
    "skills",
    "context",
    "untrusted_input",
    "system_prompt",
    "delegation_policy",
    "memory_policy",
    "audit_policy",
    "autonomy",
    "execution_limits",
)


@pytest.mark.parametrize("name", REFERENCE_AGENTS)
def test_reference_agent_matches_its_predefined_role_except_the_listed_changes(
    name: str, no_deprecated_role: None
) -> None:
    reference = resolve(_folder_agent(name))
    predefined = _predefined(name)
    changes = _POLICY_CHANGES.get(name, {})

    for field in _COMPARED_FIELDS:
        before = getattr(predefined, field)
        after = getattr(reference, field)
        if field in changes:
            assert (before, after) == changes[field], field
        elif field in ("tools", "permissions", "skills"):
            assert set(after) == set(before), field
        else:
            assert after == before, field


@pytest.mark.parametrize("name", REFERENCE_AGENTS)
def test_reference_agent_keeps_every_escalation_condition_and_adds_only_the_listed_ones(
    name: str, no_deprecated_role: None
) -> None:
    reference = resolve(_folder_agent(name))
    predefined = _predefined(name)

    before = set(predefined.escalation_rules["conditions"])
    after = set(reference.escalation_rules["conditions"])

    assert after - before == _ADDED_CONDITIONS.get(name, set())
    assert before <= after
    assert reference.escalation_rules["escalate_to"] == "human"


@pytest.mark.parametrize("name", REFERENCE_AGENTS)
def test_reference_agent_describes_inherited_conditions_with_its_parents_wording(
    name: str, no_deprecated_role: None
) -> None:
    reference = resolve(_folder_agent(name))
    predefined = _predefined(name)

    for condition in ("required_tool_missing", "confidence_below_threshold"):
        assert _description(reference, condition) == _base_description(condition)
    for condition, text in predefined.escalation_rules["descriptions"].items():
        if condition in _REWORDED_INHERITED.get(name, set()):
            continue
        assert _description(reference, condition) == " ".join(str(text).split())


def test_sales_agent_keeps_its_catalog_threshold_as_its_own_condition(
    no_deprecated_role: None,
) -> None:
    """The predefined sales-agent reworded `confidence_below_threshold` as a
    catalog-match threshold. The reference agent keeps that meaning under a
    condition of its own, so nothing the model was told is lost."""
    reference = resolve(_folder_agent("sales-agent"))
    predefined = _predefined("sales-agent")

    assert _description(reference, "catalog_match_below_threshold") == _description(
        predefined, "confidence_below_threshold"
    )


def test_developer_agent_inherits_the_operator_agents_turn_budget(
    no_deprecated_role: None,
) -> None:
    """The reference developer-agent declares no limits: every one comes from
    the reference operator-agent, which ADR-010 leaves at 30 s until the
    composition scenarios (#225) decide with live data."""
    developer = resolve(_folder_agent("developer-agent"))
    operator = resolve(_folder_agent("operator-agent"))

    assert developer.execution_limits == operator.execution_limits
    assert operator.execution_limits == {
        "tool_call_timeout_s": 10,
        "total_execution_timeout_s": 30,
        "max_tool_calls": 10,
    }
