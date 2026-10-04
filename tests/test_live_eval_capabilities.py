"""Live eval: the behavior capability suite (#224, ADR-010 part 1).

Runs every scenario shipped in `agents_system.evals.capabilities` against a
real model, `EVAL_RUNS` times each (default 5), on its capability's minimal
fixture agent and in-memory reference backends. Each scenario is held to its
#81 gate: 80% for a happy-path scenario, 100% of exercised runs (and at least
one exercised) for a guardrail scenario.

Opt-in (`uv run --env-file .env pytest -m live`); needs no database. The `commands` capability
needs `bwrap` and coreutils `factor` and is skipped, naming them, without
either. See docs/platform/live-eval.md.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from langchain_core.language_models import BaseChatModel

from agents_system.evals.capabilities import (
    CapabilityScenario,
    load_capability_scenarios,
    missing_requirements,
    run_capability_scenario,
)
from agents_system.evals.provider import build_eval_model
from agents_system.evals.reporting import write_results

pytestmark = pytest.mark.live

_RUNS = int(os.environ.get("EVAL_RUNS", "5"))

_CASES = load_capability_scenarios()


@pytest.fixture
async def eval_model() -> AsyncIterator[tuple[BaseChatModel, str]]:
    """A fresh model per test: the openai_compatible model's HTTP client is
    bound to the event loop that first used it (see
    `tests/test_live_eval_composition.py`'s fixture of the same name)."""
    model, model_name = build_eval_model()
    try:
        yield model, model_name
    finally:
        client = getattr(model, "http_async_client", None)
        if client is not None:
            await client.aclose()


@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=[f"{case.capability.id}.{case.scenario.name}" for case in _CASES],
)
async def test_capability_scenario_meets_its_gate(
    case: CapabilityScenario, eval_model: tuple[BaseChatModel, str]
) -> None:
    missing = missing_requirements(case.capability)
    if missing:
        pytest.skip(f"{case.capability.id} needs {', '.join(missing)} on this host")

    model, model_name = eval_model
    result = await run_capability_scenario(
        case, model=model, model_name=model_name, runs=_RUNS
    )
    write_results([result])

    usage = result.total_usage
    tokens = usage.total_tokens if usage is not None else None
    cost = usage.cost_usd if usage is not None else None
    print(
        f"\nlive-eval: capability={case.capability.id} "
        f"scenario={result.scenario} agent={result.agent} model={result.model} "
        f"category={result.category} runs={len(result.runs)} "
        f"passed={sum(run.passed for run in result.runs)} "
        f"exercised={result.exercised_count} "
        f"success_rate={result.success_rate:.0%} threshold={result.threshold:.0%} "
        f"tokens={tokens} cost_usd={cost} "
        f"duration_s={result.total_duration_s} "
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
