"""Offline tests for the live-eval scenario schema (#169, ADR-002 E.18).

A scenario file is untrusted input off disk, same as any manifest -- these
tests pin the exact structural checks `load_scenario` performs, so a typo in
a scenario YAML fails loudly at load time instead of silently evaluating the
wrong assertions.
"""

from __future__ import annotations

import pathlib

import pytest

from agents_system.evals.schema import (
    Scenario,
    ScenarioAssertions,
    ScenarioError,
    load_scenario,
    load_scenarios,
)


def _write(tmp_path: pathlib.Path, name: str, text: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


_MINIMAL = """
role: sales-agent
turns:
  - "Do you have Item Alpha in stock?"
"""


def test_load_scenario_parses_a_minimal_valid_file(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, "minimal.yaml", _MINIMAL)

    scenario = load_scenario(path)

    assert scenario == Scenario(
        name="minimal",
        role="sales-agent",
        turns=("Do you have Item Alpha in stock?",),
        assertions=ScenarioAssertions(),
        description="",
        client=None,
        granted_permissions=None,
        source=path,
    )


def test_load_scenario_name_defaults_to_the_file_stem(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, "sales_agent_smoke.yaml", _MINIMAL)

    scenario = load_scenario(path)

    assert scenario.name == "sales_agent_smoke"


def test_load_scenario_explicit_name_overrides_the_file_stem(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(
        tmp_path,
        "minimal.yaml",
        _MINIMAL + "\nname: custom-name\n",
    )

    scenario = load_scenario(path)

    assert scenario.name == "custom-name"


def test_load_scenario_parses_every_assertion_field(tmp_path: pathlib.Path) -> None:
    text = """
role: sales-agent
description: "Smoke test"
turns:
  - "Hi"
  - "Do you have Item Alpha?"
assertions:
  tools_called: [catalog_search]
  tools_not_called: [order_writer, escalation_notifier]
  permission_denied: false
  escalation_expected: false
granted_permissions: [read:catalog]
client: deployment-id
"""
    path = _write(tmp_path, "full.yaml", text)

    scenario = load_scenario(path)

    assert scenario.description == "Smoke test"
    assert scenario.turns == ("Hi", "Do you have Item Alpha?")
    assert scenario.assertions == ScenarioAssertions(
        tools_called=("catalog_search",),
        tools_not_called=("order_writer", "escalation_notifier"),
        permission_denied=False,
        escalation_expected=False,
    )
    assert scenario.granted_permissions == ("read:catalog",)
    assert scenario.client == "deployment-id"


@pytest.mark.parametrize(
    "text",
    [
        "turns:\n  - hi\n",  # no role at all
        'role: ""\nturns:\n  - hi\n',  # empty role
        "role: 5\nturns:\n  - hi\n",  # non-string role
    ],
)
def test_load_scenario_rejects_a_missing_or_invalid_role(
    tmp_path: pathlib.Path, text: str
) -> None:
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="role"):
        load_scenario(path)


def test_load_scenario_rejects_empty_turns(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, "bad.yaml", "role: sales-agent\nturns: []\n")

    with pytest.raises(ScenarioError, match="turns"):
        load_scenario(path)


def test_load_scenario_rejects_non_string_turns(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, "bad.yaml", "role: sales-agent\nturns:\n  - hi\n  - 5\n")

    with pytest.raises(ScenarioError, match="turns"):
        load_scenario(path)


def test_load_scenario_rejects_a_non_boolean_escalation_expected(
    tmp_path: pathlib.Path,
) -> None:
    text = """
role: sales-agent
turns:
  - hi
assertions:
  escalation_expected: "yes"
"""
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="escalation_expected"):
        load_scenario(path)


def test_load_scenario_rejects_a_non_mapping_assertions_block(
    tmp_path: pathlib.Path,
) -> None:
    text = (
        "role: sales-agent\nturns:\n  - hi\nassertions:\n  - not\n  - a\n  - mapping\n"
    )
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="assertions.*mapping"):
        load_scenario(path)


def test_load_scenario_rejects_a_non_string_client(tmp_path: pathlib.Path) -> None:
    path = _write(
        tmp_path, "bad.yaml", "role: sales-agent\nturns:\n  - hi\nclient: 5\n"
    )

    with pytest.raises(ScenarioError, match="client"):
        load_scenario(path)


def test_load_scenario_rejects_a_non_mapping_document(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, "bad.yaml", "- just\n- a\n- list\n")

    with pytest.raises(ScenarioError, match="mapping"):
        load_scenario(path)


def test_load_scenario_rejects_invalid_yaml(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, "bad.yaml", "role: [unterminated\n")

    with pytest.raises(ScenarioError, match="YAML"):
        load_scenario(path)


def test_load_scenarios_loads_every_file_in_a_directory_sorted(
    tmp_path: pathlib.Path,
) -> None:
    _write(tmp_path, "b_scenario.yaml", _MINIMAL)
    _write(tmp_path, "a_scenario.yml", _MINIMAL)
    (tmp_path / "not_a_scenario.txt").write_text("ignored", encoding="utf-8")

    scenarios = load_scenarios(tmp_path)

    assert [s.name for s in scenarios] == ["a_scenario", "b_scenario"]
