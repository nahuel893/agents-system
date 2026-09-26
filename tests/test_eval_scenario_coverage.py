"""Offline contract test: every role issue #88 named needs live escalation
coverage (#89 review, finding 2).

Issue #88's third acceptance criterion asked for "one scenario per exposed
role (data, developer, sales, summary) ... added or checked for escalation."
PR #89 shipped rendering + description parsing but never added one: none of
`data_agent_no_fabrication.yaml`, `developer_agent_no_fabrication.yaml`, or
`summary_agent_no_fabrication.yaml` assert `escalation_expected`, and
sales-agent's only scenario (`sales_agent_smoke.yaml`) asserts
`escalation_expected: false` -- the opposite of an escalation-path scenario.

This is a structural, offline check over the scenario files on disk (like
`test_role_contract_suite.py`'s descriptions contract): it never talks to a
model, and it never asserts a scenario's *success rate* -- `pytest -m live`
(`tests/test_live_eval_roles.py`) already owns running it and reporting
that, a probabilistic signal deliberately excluded from CI.
"""

from __future__ import annotations

import pathlib

from agents_system.evals.schema import load_scenarios

_SCENARIOS_DIR = pathlib.Path(__file__).resolve().parents[1] / "evals" / "scenarios"

#: Issue #88's own list -- the roles the escalation-description work made
#: newly relevant, whose live coverage this test guards.
_EXPOSED_ROLES = ("data-agent", "developer-agent", "sales-agent", "summary-agent")


def test_every_exposed_role_has_an_escalation_asserting_scenario() -> None:
    scenarios = load_scenarios(_SCENARIOS_DIR)

    roles_with_escalation_coverage = {
        scenario.role
        for scenario in scenarios
        if scenario.assertions.escalation_expected is True
    }

    missing = [
        role for role in _EXPOSED_ROLES if role not in roles_with_escalation_coverage
    ]
    assert not missing, (
        "the following roles have no evals/scenarios/*.yaml file asserting "
        f"escalation_expected: true: {missing} (issue #88's third acceptance "
        "criterion)"
    )
