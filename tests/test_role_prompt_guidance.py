"""Prompt guidance for the two roles whose live scenarios failed (#157).

The live scenarios (`evals/scenarios/developer_agent_escalation.yaml`,
`data_agent_no_fabrication.yaml`) measure model behavior; these offline tests
pin the wording that behavior was fixed with, so a later prompt edit cannot
silently drop it. They read the prompt the model actually receives
(`factory._compose_prompt`), not the source files.

Diagnosis behind the wording, from the live transcripts:

- developer-agent: asked to check three release-note paths that do not exist,
  the model read all three (`not_found`), listed the directory, queried the
  knowledge base, read the one file that exists, and then ANSWERED "there are
  no release notes". Every step was honest, and none of it was an escalation:
  `repeated_command_failure` was worded around "retried a failing command", so
  the model did not recognise "several different candidate paths, all failed"
  as that condition, and role.md never said a failed task must reach a human.
- data-agent: after `catalog_search` it kept calling `knowledge_retrieval` and
  `run_report` with reworded input looking for a price and a stock figure no
  tool holds, until the turn budget ended.
"""

from __future__ import annotations

import pathlib

import pytest

from agents_system.harness.factory import _compose_prompt
from agents_system.harness.loader import RootConfig, resolve

#: ADR-010 (#222): these tests resolve concrete predefined roles by name, which
#: is deprecated. They move to `agent` or the reference agents in
#: `examples/agents/` when the roles leave `platform/roles/` (#229).
pytestmark = pytest.mark.filterwarnings(
    "ignore::agents_system.harness.loader.PredefinedRoleDeprecationWarning"
)

_REAL_PLATFORM_ROOT = pathlib.Path(__file__).parent.parent / "platform"


def _prompt(role: str) -> str:
    definition = resolve(role, roots=RootConfig(platform_root=_REAL_PLATFORM_ROOT))
    return _compose_prompt(definition, ())


def test_developer_agent_role_says_a_failed_task_must_reach_a_human() -> None:
    prompt = _prompt("developer-agent")

    # The instruction lives in the role prose, above the generated
    # escalation block, and names the tool: telling the user is not enough.
    role_prose = prompt.split("## escalation rules")[0]
    assert "escalation_notifier" in role_prose
    assert "not_found" in role_prose


def test_developer_agent_treats_several_failed_candidate_paths_as_repeated_failure() -> (
    None
):
    definition = resolve(
        "developer-agent", roots=RootConfig(platform_root=_REAL_PLATFORM_ROOT)
    )

    description = definition.escalation_rules["descriptions"][
        "repeated_command_failure"
    ]

    # Keeps the original meaning ...
    assert "retried" in description
    # ... and names the shape the live transcripts showed: different
    # candidate paths, each failing, is the same loop as one command retried.
    assert "candidate paths" in description
    assert "not_found" in description


def test_data_agent_stops_when_no_tool_holds_the_figure() -> None:
    prompt = _prompt("data-agent")

    assert "no tool holds" in prompt
    # One attempt per source, not a loop of reworded queries.
    assert "reworded" in prompt


def test_data_agent_stop_rule_allows_one_lookup_per_distinct_entity() -> None:
    section = " ".join(
        _prompt("data-agent")
        .split("## stopping when no tool holds the answer")[1]
        .split()
    )

    # The stop rule targets repeated calls for the SAME missing fact, so a
    # question about two SKUs (or a customer and a SKU) still gets one lookup
    # each, and an incomplete result may be followed up.
    assert "same missing" in section
    assert "distinct" in section
    assert "follow-up" in section
    # The blanket wording that forbade a second call to a tool is gone.
    assert "at most once per question" not in section


def test_developer_agent_allows_one_bounded_discovery_step_before_escalating() -> None:
    prompt = _prompt("developer-agent")
    section = " ".join(
        prompt.split("## when the task cannot be finished")[1]
        .split("## escalation rules")[0]
        .split()
    )

    # A listing or knowledge-base lookup that can locate the file is a
    # permitted discovery step; open-ended probing is not.
    assert "discovery" in section
    assert "one bounded" in section
    # Escalation is due when the task is STILL unanswered after that step.
    assert "still" in section
    # A single policy refusal escalates immediately.
    assert "command_refused_by_policy" in section
    assert "immediately" in section
    # The blanket ban on looking in the directory is gone.
    assert "Do not keep probing with neighbours" not in section


@pytest.mark.parametrize("role", ["developer-agent", "data-agent"])
def test_guidance_does_not_leak_design_notes(role: str) -> None:
    assert "## design notes" not in _prompt(role)
