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
    CATEGORY_GUARDRAIL,
    CATEGORY_HAPPY_PATH,
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


def test_load_scenario_rejects_an_unknown_granted_permission_name(
    tmp_path: pathlib.Path,
) -> None:
    text = (
        "role: sales-agent\nturns:\n  - hi\n"
        "granted_permissions: [not-a-real-permission]\n"
    )
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError) as exc_info:
        load_scenario(path)

    assert str(path) in str(exc_info.value)
    assert "not-a-real-permission" in str(exc_info.value)


def test_load_scenario_rejects_a_non_list_granted_permissions(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(
        tmp_path,
        "bad.yaml",
        "role: sales-agent\nturns:\n  - hi\ngranted_permissions: 5\n",
    )

    with pytest.raises(ScenarioError, match="granted_permissions"):
        load_scenario(path)


def test_load_scenario_accepts_an_explicit_empty_granted_permissions_list(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(
        tmp_path,
        "empty_grants.yaml",
        "role: sales-agent\nturns:\n  - hi\ngranted_permissions: []\n",
    )

    scenario = load_scenario(path)

    assert scenario.granted_permissions == ()


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


# ---------------------------------------------------------------------------
# #81 -- scenario category and happy-path threshold override
# ---------------------------------------------------------------------------


def test_load_scenario_category_defaults_to_happy_path(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "minimal.yaml", _MINIMAL)

    scenario = load_scenario(path)

    assert scenario.category == CATEGORY_HAPPY_PATH
    assert scenario.threshold is None
    assert scenario.threshold_reason is None


def test_load_scenario_accepts_an_explicit_guardrail_category(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "guardrail.yaml", _MINIMAL + "\ncategory: guardrail\n")

    scenario = load_scenario(path)

    assert scenario.category == CATEGORY_GUARDRAIL


def test_load_scenario_rejects_an_unknown_category(tmp_path: pathlib.Path) -> None:
    path = _write(tmp_path, "bad.yaml", _MINIMAL + "\ncategory: made-up\n")

    with pytest.raises(ScenarioError, match="category"):
        load_scenario(path)


def test_load_scenario_accepts_a_happy_path_threshold_override_with_a_reason(
    tmp_path: pathlib.Path,
) -> None:
    text = (
        _MINIMAL
        + "\ncategory: happy_path\nthreshold: 0.6\n"
        + "threshold_reason: small model, tool-calling is flaky by design\n"
    )
    path = _write(tmp_path, "override.yaml", text)

    scenario = load_scenario(path)

    assert scenario.threshold == 0.6
    assert scenario.threshold_reason == ("small model, tool-calling is flaky by design")


def test_load_scenario_rejects_a_threshold_without_a_reason(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "bad.yaml", _MINIMAL + "\nthreshold: 0.6\n")

    with pytest.raises(ScenarioError, match="threshold_reason"):
        load_scenario(path)


def test_load_scenario_rejects_a_reason_without_a_threshold(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "bad.yaml", _MINIMAL + "\nthreshold_reason: because\n")

    with pytest.raises(ScenarioError, match="threshold"):
        load_scenario(path)


def test_load_scenario_rejects_a_threshold_on_a_guardrail_scenario(
    tmp_path: pathlib.Path,
) -> None:
    text = (
        _MINIMAL + "\ncategory: guardrail\nthreshold: 0.9\nthreshold_reason: because\n"
    )
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="guardrail"):
        load_scenario(path)


@pytest.mark.parametrize("bad_threshold", [0, -0.1, 1.5, "high"])
def test_load_scenario_rejects_an_out_of_range_or_non_numeric_threshold(
    tmp_path: pathlib.Path, bad_threshold: object
) -> None:
    text = _MINIMAL + f"\nthreshold: {bad_threshold!r}\nthreshold_reason: because\n"
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="threshold"):
        load_scenario(path)


# ---------------------------------------------------------------------------
# #76 -- turn_permissions (Layer-2 revalidation) and execution_limits_override
# ---------------------------------------------------------------------------


def test_load_scenario_parses_turn_permissions(tmp_path: pathlib.Path) -> None:
    text = _MINIMAL + "\nturn_permissions: [read:catalog]\n"
    path = _write(tmp_path, "turn_perms.yaml", text)

    scenario = load_scenario(path)

    assert scenario.turn_permissions == ("read:catalog",)


def test_load_scenario_turn_permissions_defaults_to_none(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "minimal.yaml", _MINIMAL)

    scenario = load_scenario(path)

    assert scenario.turn_permissions is None


def test_load_scenario_accepts_an_explicit_empty_turn_permissions_list(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "empty.yaml", _MINIMAL + "\nturn_permissions: []\n")

    scenario = load_scenario(path)

    assert scenario.turn_permissions == ()


def test_load_scenario_rejects_an_unknown_turn_permission_name(
    tmp_path: pathlib.Path,
) -> None:
    text = _MINIMAL + "\nturn_permissions: [not-a-real-permission]\n"
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError) as exc_info:
        load_scenario(path)

    assert "not-a-real-permission" in str(exc_info.value)
    assert "turn_permissions" in str(exc_info.value)


def test_load_scenario_rejects_a_non_list_turn_permissions(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "bad.yaml", _MINIMAL + "\nturn_permissions: 5\n")

    with pytest.raises(ScenarioError, match="turn_permissions"):
        load_scenario(path)


def test_load_scenario_parses_execution_limits_override(
    tmp_path: pathlib.Path,
) -> None:
    text = (
        _MINIMAL + "\nexecution_limits_override:\n"
        "  max_tool_calls: 2\n  tool_call_timeout_s: 3\n"
    )
    path = _write(tmp_path, "limits.yaml", text)

    scenario = load_scenario(path)

    assert scenario.execution_limits_override == {
        "max_tool_calls": 2.0,
        "tool_call_timeout_s": 3.0,
    }


def test_load_scenario_execution_limits_override_defaults_to_none(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "minimal.yaml", _MINIMAL)

    scenario = load_scenario(path)

    assert scenario.execution_limits_override is None


def test_load_scenario_rejects_an_unknown_execution_limit_key(
    tmp_path: pathlib.Path,
) -> None:
    text = _MINIMAL + "\nexecution_limits_override:\n  made_up_limit: 1\n"
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="execution_limits_override"):
        load_scenario(path)


def test_load_scenario_rejects_a_non_mapping_execution_limits_override(
    tmp_path: pathlib.Path,
) -> None:
    text = _MINIMAL + "\nexecution_limits_override: [1, 2]\n"
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="execution_limits_override"):
        load_scenario(path)


@pytest.mark.parametrize("bad_value", [0, -1, "fast"])
def test_load_scenario_rejects_a_non_positive_or_non_numeric_execution_limit(
    tmp_path: pathlib.Path, bad_value: object
) -> None:
    text = _MINIMAL + f"\nexecution_limits_override:\n  max_tool_calls: {bad_value!r}\n"
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="execution_limits_override"):
        load_scenario(path)


# ---------------------------------------------------------------------------
# #76 -- the five new assertion fields (tool_blocked, limit_reached,
# audit_event, not_executed, guardrail_exercised)
# ---------------------------------------------------------------------------


def test_load_scenario_parses_the_five_new_assertion_fields(
    tmp_path: pathlib.Path,
) -> None:
    text = """
role: sales-agent
turns:
  - hi
assertions:
  tool_blocked: [order_writer]
  limit_reached: true
  audit_event: [runtime_timeout]
  not_executed: [read_file]
  guardrail_exercised: [knowledge_retrieval]
"""
    path = _write(tmp_path, "new_assertions.yaml", text)

    scenario = load_scenario(path)

    assert scenario.assertions.tool_blocked == ("order_writer",)
    assert scenario.assertions.limit_reached is True
    assert scenario.assertions.audit_event == ("runtime_timeout",)
    assert scenario.assertions.not_executed == ("read_file",)
    assert scenario.assertions.guardrail_exercised == ("knowledge_retrieval",)


def test_load_scenario_new_assertion_fields_default_empty(
    tmp_path: pathlib.Path,
) -> None:
    path = _write(tmp_path, "minimal.yaml", _MINIMAL)

    scenario = load_scenario(path)

    assert scenario.assertions.tool_blocked == ()
    assert scenario.assertions.limit_reached is None
    assert scenario.assertions.audit_event == ()
    assert scenario.assertions.not_executed == ()
    assert scenario.assertions.guardrail_exercised == ()


def test_load_scenario_rejects_a_non_boolean_limit_reached(
    tmp_path: pathlib.Path,
) -> None:
    text = _MINIMAL + '\nassertions:\n  limit_reached: "yes"\n'
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match="limit_reached"):
        load_scenario(path)


@pytest.mark.parametrize(
    "field", ["tool_blocked", "audit_event", "not_executed", "guardrail_exercised"]
)
def test_load_scenario_rejects_a_non_list_new_tuple_assertion(
    tmp_path: pathlib.Path, field: str
) -> None:
    text = _MINIMAL + f"\nassertions:\n  {field}: 5\n"
    path = _write(tmp_path, "bad.yaml", text)

    with pytest.raises(ScenarioError, match=field):
        load_scenario(path)


def test_execution_limits_override_docstring_discloses_it_bypasses_the_ceiling_invariant() -> (
    None
):
    """PR #102 review fix (#76) -- `execution_limits_override` merges
    straight into a scenario's resolved `execution_limits`
    (`runner._build_equipped`) with no equivalent to
    `harness.loader._validate_execution_limits`'s "Deployments may only
    restrict, not elevate" ceiling check, unlike every other
    `execution_limits` boundary in the codebase (a real deployment override,
    an importer agent). That gap is intentional here: a guardrail scenario
    sometimes needs to RAISE a limit past the platform/role ceiling (e.g.
    `06_tool_call_timeout.yaml` raises `total_execution_timeout_s` past
    operator-agent's own tightened baseline) so a slow real model's own
    latency does not swallow the very timeout being tested before it can be
    observed. Intentional is fine; undocumented is not -- this pins that the
    module says so in its own words, so a future reader never mistakes
    "accepted" for "bounded the same way `_validate_execution_limits`
    enforces everywhere else".
    """
    import inspect

    from agents_system.evals import schema

    source = pathlib.Path(inspect.getfile(schema)).read_text(encoding="utf-8")
    assert "_validate_execution_limits" in source
    assert "eval-only escape hatch" in source.lower()
