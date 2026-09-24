"""Live-eval smoke test: sales-agent against a real model (#169, ADR-002 E.18).

Excluded from the default run -- select it explicitly with `pytest -m live`.
The provider defaults to local Ollama (`ollama serve`, configured model
already pulled) but is switchable via `EVAL_PROVIDER` -- see
`docs/platform/live-eval.md` for how to run it, the provider switch, and
where results land.

This test proves the PIPELINE runs end to end against a real model, not that
the model is good: a low success rate on a small model (e.g. `qwen2.5:3b`) is
expected and acceptable -- ADR-002 E.18 names it a deliberate floor/regression
case. Only structural completeness is asserted here; the success rate is
reported, not gated.
"""

from __future__ import annotations

import pathlib

import pytest

from agents_system.evals.provider import build_eval_model
from agents_system.evals.reporting import write_results
from agents_system.evals.runner import run_scenario
from agents_system.evals.schema import load_scenario
from agents_system.harness.loader import RootConfig

pytestmark = pytest.mark.live

_SCENARIO_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "evals"
    / "scenarios"
    / "sales_agent_smoke.yaml"
)
_RUNS = 5


async def test_sales_agent_smoke_scenario_runs_against_a_real_model() -> None:
    from conftest import build_test_registry

    scenario = load_scenario(_SCENARIO_PATH)
    model, model_name = build_eval_model()

    result = await run_scenario(
        scenario,
        model=model,
        model_name=model_name,
        registry=build_test_registry(),
        roots=RootConfig(),
        runs=_RUNS,
    )

    write_results([result])

    print(
        f"\nlive-eval: scenario={result.scenario} role={result.role} "
        f"model={result.model} runs={len(result.runs)} "
        f"success_rate={result.success_rate:.0%}"
    )
    for index, run in enumerate(result.runs):
        status = "pass" if run.passed else "fail"
        detail = run.error or "; ".join(f"{f.kind}: {f.detail}" for f in run.failures)
        print(f"  run {index}: {status} {detail}".rstrip())

    # Structural completeness, not a quality gate: proves every run actually
    # executed (no runner-level crash swallowed the whole scenario) and that
    # results were written for the PR record -- not that the model passed.
    assert len(result.runs) == _RUNS
