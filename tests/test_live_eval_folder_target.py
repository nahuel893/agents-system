"""Live eval: one scenario through an agent-folder target (#221, ADR-010).

Runs `tests/fixtures/agents/eval_targets/stock_lookup.yaml` on the real
reference model. Opt-in (`pytest -m live`); see docs/platform/live-eval.md.
"""

from __future__ import annotations

import pathlib

import pytest

from agents_system.evals.provider import build_eval_model
from agents_system.evals.reporting import write_results
from agents_system.evals.runner import AGENT_KIND_FOLDER, run_scenario
from agents_system.evals.schema import load_scenario

pytestmark = pytest.mark.live

_ROOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "agents" / "eval_targets"


async def test_folder_target_scenario_runs_against_a_real_model() -> None:
    from conftest import build_test_registry

    scenario = load_scenario(_ROOT / "stock_lookup.yaml")
    model, model_name = build_eval_model()
    runs = 3

    result = await run_scenario(
        scenario,
        model=model,
        model_name=model_name,
        registry=build_test_registry(),
        agents_root=_ROOT,
        runs=runs,
    )
    write_results([result])

    print(
        f"\nlive-eval: scenario={result.scenario} agent={result.agent} "
        f"kind={result.agent_kind} model={result.model} runs={len(result.runs)} "
        f"success_rate={result.success_rate:.0%} "
        f"gate={'pass' if result.gate.passed else 'fail'}"
    )
    for index, run in enumerate(result.runs):
        detail = run.error or "; ".join(f"{f.kind}: {f.detail}" for f in run.failures)
        print(f"  run {index}: {'pass' if run.passed else 'fail'} {detail}".rstrip())

    assert result.agent == "stock-bot"
    assert result.agent_kind == AGENT_KIND_FOLDER
    assert len(result.runs) == runs
    assert result.gate.passed, result.gate.reason
