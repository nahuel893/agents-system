"""Live-eval driver for the seven remaining roles' scenarios (#171).

Excluded from the default run -- select it explicitly with `pytest -m live`,
exactly like `tests/test_live_eval_sales_agent.py`. See
`docs/platform/live-eval.md` for the provider switch and where results land.

This loads every scenario under `evals/scenarios/` EXCEPT
`sales_agent_smoke.yaml` (already owned by #169 /
`tests/test_live_eval_sales_agent.py`) and runs each one `EVAL_RUNS` times
(default 5) against a real model, through the SAME `run_scenario` pipeline,
using `agentsys.evals.live_registry.build_live_registry_factory` so every
run gets its own, unshared reference-backend instances (PR #176 review note
1 -- see `src/agentsys/evals/live_registry.py`).

This proves the pipeline runs end to end for each role, not that the model
is good: a low success rate is an expected, useful signal (which role/tool
combination a given model struggles with), never a test failure -- only
structural completeness is asserted (`len(result.runs) == runs`), matching
`tests/test_live_eval_sales_agent.py`'s own contract.
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

from agentsys.connectors.operator import SandboxPolicy, TerminalPolicy
from agentsys.evals.live_registry import build_live_registry_factory
from agentsys.evals.provider import build_eval_model
from agentsys.evals.reporting import write_results
from agentsys.evals.runner import run_scenario
from agentsys.evals.schema import Scenario, load_scenarios
from agentsys.harness.loader import RootConfig

pytestmark = pytest.mark.live

_SCENARIOS_DIR = pathlib.Path(__file__).resolve().parents[1] / "evals" / "scenarios"

#: The disposable local dev Postgres this issue's demo data was loaded into
#: (docker-compose's dev-only credentials, not a secret).
_DEMO_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agentsys_demo"
)

_DEFAULT_RUNS = 5
_RUNS = int(os.environ.get("EVAL_RUNS", str(_DEFAULT_RUNS)))

#: These two roles' tools (`use_term`, `read_file`) run through bwrap
#: sandboxing (ADR-002 C.14) -- skip cleanly, with a clear reason, for
#: exactly these when bwrap is unavailable. Every other role never touches
#: the sandbox, so it is never skipped for this reason.
_OPERATOR_SHAPED_ROLES = frozenset({"operator-agent", "developer-agent"})

#: sales_agent_smoke.yaml is #169's own smoke scenario, already run by
#: tests/test_live_eval_sales_agent.py -- loading it again here would run
#: it twice under two different drivers.
_SCENARIOS: tuple[Scenario, ...] = tuple(
    scenario
    for scenario in load_scenarios(_SCENARIOS_DIR)
    if scenario.source is not None and scenario.source.name != "sales_agent_smoke.yaml"
)


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh `AsyncEngine` per test function.

    Deliberately function-scoped, not module-scoped: pytest-asyncio gives
    each test function its own event loop by default
    (`asyncio_default_test_loop_scope` is unset -- see `pyproject.toml`), but
    an `AsyncEngine`'s pooled `asyncpg` connections are bound to whichever
    loop was running when they were created. A module-scoped engine whose
    pool was warmed up on scenario N's loop and then reused on scenario
    N+1's *different* loop corrupts the pooled connection -- observed
    directly as `InterfaceError: cannot perform operation: another operation
    is in progress` (and, when the corruption hits mid-await, "attached to a
    different loop") on every DB-backed scenario that happened to reuse a
    stale connection, well before this was ever a question of the role or
    the model's own behavior. Building a fresh engine (and disposing it)
    inside each test's own loop avoids the whole class of failure; the
    pool's small per-test connection overhead is negligible for a manual /
    nightly live-eval run.
    """
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
    """A dedicated, disposable sandbox root for `operator-agent` /
    `developer-agent` scenarios -- never the process's own cwd or `$HOME`
    (`TerminalPolicy.__post_init__` refuses both). Seeded with exactly one
    known file so the "happy"/"no fabrication" scenarios have a known-present
    and a known-absent target.
    """
    with tempfile.TemporaryDirectory(prefix="agentsys-live-eval-operator-") as tmp:
        root = pathlib.Path(tmp)
        (root / "notes.txt").write_text(
            "Sprint status: green. No blockers.", encoding="utf-8"
        )
        yield root


@pytest.fixture(scope="module")
def terminal_policy(operator_workspace: pathlib.Path) -> TerminalPolicy:
    return TerminalPolicy(
        root=operator_workspace,
        allowed_commands=frozenset({"ls", "cat"}),
        sandbox=SandboxPolicy(),
    )


@pytest.mark.parametrize(
    "scenario", _SCENARIOS, ids=[scenario.name for scenario in _SCENARIOS]
)
async def test_role_scenario_runs_against_a_real_model(
    scenario: Scenario,
    engine: AsyncEngine,
    eval_model: tuple[BaseChatModel, str],
    terminal_policy: TerminalPolicy,
) -> None:
    if scenario.role in _OPERATOR_SHAPED_ROLES and shutil.which("bwrap") is None:
        pytest.skip(
            reason="operator/developer roles need bwrap, which is not installed"
        )

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
    # results were written for the record -- never that the model passed.
    assert len(result.runs) == _RUNS
