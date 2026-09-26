"""Live guardrail suite driver (#76) -- a real LLM as the adversary,
assertions on harness behavior.

Excluded from the default run -- select it explicitly with `pytest -m live`,
exactly like `tests/test_live_eval_roles.py` and
`tests/test_live_eval_sales_agent.py`. See `docs/platform/live-eval.md` for
the provider switch, the new assertion types, and where results land.

This loads every scenario under `evals/scenarios/guardrails/` -- a dedicated
subdirectory `load_scenarios(evals/scenarios/...)`'s own non-recursive glob
never touches, so it never overlaps `tests/test_live_eval_roles.py`'s 21
scenarios or `tests/test_eval_scenario_coverage.py`'s structural check. Each
scenario proves ONE Phase-2 guardrail (docs/delivery/live-test-plan.md) holds
against a real, adversarial-primed model: which tools were bound and
executed, which calls were blocked and why, which audit events landed, and
whether a limit or timeout node fired -- never the model's own wording.

Scenario 1 (grant ceiling, Layer 1) and scenario 7 (escalation) are already
covered by pre-existing scenarios and are not duplicated here:
scenario 1 by every `evals/scenarios/*_boundary.yaml` file (#171/#178,
comments corrected for #76), scenario 7 by
`data_agent_escalation.yaml`/`sales_agent_escalation.yaml`/
`developer_agent_escalation.yaml`/`summary_agent_escalation.yaml`
(`category: guardrail`, `escalation_expected: true`, already gated by #81's
"exercised" machinery). Scenario 8 (no-fabrication under pressure) is
explicitly out of scope for #76 -- see the issue's own acceptance criteria
(scenarios 1-7) and `*_no_fabrication.yaml`'s existing coverage.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator

import pytest
from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents_system.connectors.operator import SandboxPolicy, TerminalPolicy
from agents_system.evals.live_registry import build_live_registry_factory
from agents_system.evals.provider import build_eval_model
from agents_system.evals.reporting import write_results
from agents_system.evals.runner import run_scenario
from agents_system.evals.schema import Scenario, load_scenarios
from agents_system.harness.loader import RootConfig

pytestmark = pytest.mark.live

_SCENARIOS_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "evals" / "scenarios" / "guardrails"
)

#: The disposable local dev Postgres this issue's demo data was loaded into
#: (docker-compose's dev-only credentials, not a secret) -- same database
#: `tests/test_live_eval_roles.py` already points at.
_DEMO_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo"
)

#: #76's own suggested default -- lower than the other live drivers' 5,
#: since a guardrail run's pass rule (100% of exercised runs) needs fewer
#: repetitions to be informative than a happy-path success RATE does.
_DEFAULT_RUNS = 3
_RUNS = int(os.environ.get("EVAL_RUNS", str(_DEFAULT_RUNS)))

#: `operator-agent` is the only role this suite's scenarios use that needs
#: the bwrap sandbox (`read_file`, `use_term`) -- skip cleanly, with a clear
#: reason, when bwrap is unavailable, the same posture
#: `tests/test_live_eval_roles.py` already takes for its own operator/
#: developer scenarios.
_SANDBOXED_ROLES = frozenset({"operator-agent", "developer-agent"})

_SCENARIOS: tuple[Scenario, ...] = load_scenarios(_SCENARIOS_DIR)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh `AsyncEngine` per test function -- see
    `tests/test_live_eval_roles.py::engine`'s docstring for why this must
    stay function-scoped (a pooled asyncpg connection is bound to whichever
    event loop was running when it was created)."""
    eng = create_async_engine(_DEMO_DATABASE_URL)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture(scope="module")
def eval_model() -> tuple[BaseChatModel, str]:
    return build_eval_model()


@pytest.fixture(scope="module")
def operator_workspace() -> Iterator[pathlib.Path]:
    """A dedicated, disposable sandbox root for scenario 4 (`read_file`
    containment) and scenario 6 (`use_term` timeout) -- never the process's
    own cwd or `$HOME` (`TerminalPolicy.__post_init__` refuses both)."""
    with tempfile.TemporaryDirectory(prefix="agents_system-live-guardrails-") as tmp:
        yield pathlib.Path(tmp)


@pytest.fixture(scope="module")
def terminal_policy(operator_workspace: pathlib.Path) -> TerminalPolicy:
    """Adds "sleep" to the allowlist (scenario 6's deterministic slow
    command) alongside the usual "ls"/"cat" -- no other scenario in this
    suite or `tests/test_live_eval_roles.py` ever names "sleep", so this
    addition changes nothing for anyone else.

    `timeout_s` (the sandbox's OWN per-command kill timer, distinct from
    the interceptor's `tool_call_timeout_s`) is left at a generous 30s so it
    never races scenario 6's lowered `tool_call_timeout_s` override (3s) --
    the interceptor's own timeout must be what fires, not the sandbox's.
    """
    return TerminalPolicy(
        root=operator_workspace,
        allowed_commands=frozenset({"ls", "cat", "sleep"}),
        sandbox=SandboxPolicy(),
        timeout_s=30.0,
    )


@pytest.mark.parametrize(
    "scenario", _SCENARIOS, ids=[scenario.name for scenario in _SCENARIOS]
)
async def test_guardrail_scenario_holds_against_a_real_model(
    scenario: Scenario,
    engine: AsyncEngine,
    eval_model: tuple[BaseChatModel, str],
    terminal_policy: TerminalPolicy,
) -> None:
    if scenario.role in _SANDBOXED_ROLES and shutil.which("bwrap") is None:
        pytest.skip(reason="this scenario needs bwrap, which is not installed")

    model, model_name = eval_model
    registry_factory = build_live_registry_factory(
        engine, model, terminal_policy=terminal_policy
    )

    result = await run_scenario(
        scenario,
        model=model,
        model_name=model_name,
        registry_factory=registry_factory,
        roots=RootConfig(),
        runs=_RUNS,
    )

    write_results([result])

    print(
        f"\nlive-guardrail: scenario={result.scenario} role={result.role} "
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
