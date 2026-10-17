"""Live eval: the runtime's own guarantees (#309).

Runs the scenarios under `agents_system.evals.capabilities.runtime_scenarios`
against a real model, `EVAL_RUNS` times each (default 5). They are not model
capabilities: whatever the model does, the runtime bounds the turn (an
execution limit trips). A real model shows the limit is reached by an agent
doing its job, not only by a scripted loop. Each runs on a capability's
fixture agent and in-memory backends, and is held to its #81 gate.

Opt-in (`uv run --env-file .env pytest -m live`); needs no database. See
docs/platform/live-eval.md.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from langchain_core.language_models import BaseChatModel

from agents_system.evals.capabilities import (
    CapabilityScenario,
    load_runtime_scenarios,
    run_capability_scenario,
)
from agents_system.evals.provider import build_eval_model
from agents_system.evals.reporting import write_results

pytestmark = pytest.mark.live

_RUNS = int(os.environ.get("EVAL_RUNS", "5"))

_CASES = load_runtime_scenarios()


@pytest.fixture
async def eval_model() -> AsyncIterator[tuple[BaseChatModel, str]]:
    """A fresh model per test, as in `tests/test_live_eval_capabilities.py`."""
    model, model_name = build_eval_model()
    try:
        yield model, model_name
    finally:
        client = getattr(model, "http_async_client", None)
        if client is not None:
            await client.aclose()


@pytest.mark.parametrize("case", _CASES, ids=[case.scenario.name for case in _CASES])
async def test_runtime_scenario_meets_its_gate(
    case: CapabilityScenario, eval_model: tuple[BaseChatModel, str]
) -> None:
    model, model_name = eval_model
    result = await run_capability_scenario(
        case, model=model, model_name=model_name, runs=_RUNS
    )
    write_results([result])

    print(
        f"\nlive-eval: runtime scenario={result.scenario} agent={result.agent} "
        f"model={result.model} category={result.category} runs={len(result.runs)} "
        f"passed={sum(run.passed for run in result.runs)} "
        f"exercised={result.exercised_count} "
        f"success_rate={result.success_rate:.0%} threshold={result.threshold:.0%} "
        f"gate={'pass' if result.gate.passed else 'fail'}"
    )
    for index, run in enumerate(result.runs):
        detail = run.error or "; ".join(f"{f.kind}: {f.detail}" for f in run.failures)
        status = "pass" if run.passed else "fail"
        print(
            f"  run {index}: {status} exercised={run.exercised} "
            f"tools={list(run.tools_called)} {detail}".rstrip()
        )

    assert len(result.runs) == _RUNS
    assert result.gate.passed, result.gate.reason
