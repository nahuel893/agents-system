"""Live guardrail suite driver (#76) -- a real LLM as the adversary,
assertions on harness behavior.

Excluded from the default run -- select it explicitly with `uv run --env-file .env pytest -m live`,
like every other live module. See `docs/platform/live-eval.md` for the
provider switch, the new assertion types, and where results land.

Since #223 (ADR-010) the deterministic mechanism scenarios (interceptor,
limits, sandbox, the `*_boundary` files) live in `evals/scenarios/mechanism/`
and run offline in `tests/test_mechanism_suite.py` with a scripted model, so
only the behavior scenario that needs a real model remains here.

This loads every scenario under `evals/scenarios/guardrails/` -- a dedicated
subdirectory `load_scenarios(evals/scenarios/...)`'s own non-recursive glob
never touches, so it never overlaps the per-role scenarios or
`tests/test_eval_scenario_coverage.py`'s structural check. Since #225
(ADR-010) each one names a reference agent folder (`agent:`), resolved under
`examples/agents/` like an adopter's own folder, never a predefined role by
name. Each
scenario proves ONE Phase-2 guardrail (docs/delivery/live-test-plan.md) holds
against a real, adversarial-primed model: which tools were bound and
executed, which calls were blocked and why, which audit events landed, and
whether a limit or timeout node fired -- never the model's own wording.

Scenario 1 (grant ceiling, Layer 1) and scenario 7 (escalation) are already
covered by pre-existing scenarios and are not duplicated here. Scenario 1 --
#162 retargeted every `evals/scenarios/mechanism/*_boundary.yaml` file into a live
Layer-2 revalidation scenario (the target tool IS equipped and the model
attempts it), since a tool excluded at Layer 1 is never offered to the model
to attempt, so it can never be a meaningful LIVE "exercised" (#81)
assertion -- the deterministic Layer-1 grant-ceiling fact itself now lives in
the offline
`tests/test_platform_tools_integration.py::test_boundary_scenario_permission_grant_excludes_target_tool`
(one parametrized case per role, pinned independently of the YAML files; see
that test's own header comment). Scenario 7 was covered by the per-role
`*_escalation.yaml` files (`category: guardrail`, `escalation_expected:
true`), which left the live gate with the rest of the per-role scenarios in
#225 (ADR-010): escalation is a behavior capability (#224), and the
composition scenarios (`tests/test_live_eval_composition.py`) keep an
end-to-end escalation on `sales-agent` and on `developer-agent`. Scenario 8
(no-fabrication under pressure) is explicitly out of scope for #76 -- see
the issue's own acceptance criteria (scenarios 1-7).
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncIterator

import pytest
from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents_system.evals.live_registry import (
    PROMPT_INJECTION_DOCUMENT,
    build_live_registry_factory,
)
from agents_system.evals.provider import build_eval_model
from agents_system.evals.reporting import write_results
from agents_system.evals.runner import run_scenario
from agents_system.evals.schema import Scenario, load_scenarios
from agents_system.harness.loader import RootConfig

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCENARIOS_DIR = _REPO_ROOT / "evals" / "scenarios" / "guardrails"

#: #225 -- the root every scenario's `agent:` folder is resolved under: the
#: reference agents (ADR-010), loaded like an adopter's own agent folder.
_REFERENCE_AGENTS_ROOT = _REPO_ROOT / "examples" / "agents"

#: The disposable local dev Postgres this issue's demo data was loaded into
#: (docker-compose's dev-only credentials, not a secret).
_DEMO_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo"
)

#: #76's own suggested default -- lower than the other live drivers' 5,
#: since a guardrail run's pass rule (100% of exercised runs) needs fewer
#: repetitions to be informative than a happy-path success RATE does.
_DEFAULT_RUNS = 3
_RUNS = int(os.environ.get("EVAL_RUNS", str(_DEFAULT_RUNS)))

_SCENARIOS: tuple[Scenario, ...] = load_scenarios(_SCENARIOS_DIR)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh `AsyncEngine` per test function: it must stay function-scoped,
    because pytest-asyncio gives each test its own event loop and a pooled
    asyncpg connection is bound to whichever loop was running when it was
    created (reusing it on another loop corrupts it)."""
    eng = create_async_engine(_DEMO_DATABASE_URL)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
async def eval_model() -> AsyncIterator[tuple[BaseChatModel, str]]:
    """A fresh model per test function, for the same reason as `engine`: the
    openai_compatible model's HTTP client is bound to one event loop (#145)."""
    model, model_name = build_eval_model()
    try:
        yield model, model_name
    finally:
        client = getattr(model, "http_async_client", None)
        if client is not None:
            await client.aclose()


@pytest.mark.live
@pytest.mark.parametrize(
    "scenario", _SCENARIOS, ids=[scenario.name for scenario in _SCENARIOS]
)
async def test_guardrail_scenario_holds_against_a_real_model(
    scenario: Scenario,
    engine: AsyncEngine,
    eval_model: tuple[BaseChatModel, str],
) -> None:
    model, model_name = eval_model
    registry_factory = build_live_registry_factory(
        engine,
        model,
        # #130 -- this suite is the only caller that needs scenario 3's
        # prompt-injection fixture; see `build_live_registry`'s
        # `extra_documents` docstring for why it is no longer seeded by
        # default into every live-eval registry.
        extra_documents=(PROMPT_INJECTION_DOCUMENT,),
    )

    result = await run_scenario(
        scenario,
        model=model,
        model_name=model_name,
        registry_factory=registry_factory,
        roots=RootConfig(),
        runs=_RUNS,
        agents_root=_REFERENCE_AGENTS_ROOT,
    )

    write_results([result])

    print(
        f"\nlive-guardrail: scenario={result.scenario} agent={result.agent} "
        f"model={result.model} runs={len(result.runs)} "
        f"exercised={result.exercised_count} held={result.held_count} "
        f"gate={'pass' if result.gate.passed else 'FAIL'} -- {result.gate.reason}"
    )
    for index, run in enumerate(result.runs):
        status = "held" if run.passed else ("crashed" if run.error else "broke")
        detail = run.error or "; ".join(f"{f.kind}: {f.detail}" for f in run.failures)
        print(f"  run {index}: {status} exercised={run.exercised} {detail}".rstrip())

    # Structural completeness, same as the other live drivers.
    assert len(result.runs) == _RUNS

    # The actual guardrail gate: held in every exercised run, and exercised
    # at least once (#76/#81's Principle -- a guardrail never tried proves
    # nothing). A broken guardrail is a security finding, not a flaky test
    # -- this assertion is deliberately never weakened; see the PR body for
    # this run's actual held/broken/not-exercised results.
    assert result.gate.passed, result.gate.reason
