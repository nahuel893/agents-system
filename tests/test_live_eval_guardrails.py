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
from agents_system.evals.runner import RunOutcome, ScenarioResult, run_scenario
from agents_system.evals.schema import CATEGORY_GUARDRAIL, Scenario, load_scenarios
from agents_system.harness.loader import RootConfig

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


#: PR #102 review fix (#76) -- scenarios honestly documented (see the named
#: scenario file's own header comment, and PR #102's live-run results table)
#: as not yet reliably exercisable against the pinned live-eval model:
#: `02_layer2_revalidation` never got an `order_writer` attempt in 3 live
#: runs across several turn-wording iterations
#: (deepseek/deepseek-v4-flash-0731). The mechanism itself is independently
#: proven correct offline
#: (`tests/test_eval_runner.py::
#: test_run_scenario_turn_permissions_narrows_layer2_after_layer1_equips_it`,
#: which uses a fake model that DOES attempt the call and confirms Layer 2
#: blocks it) -- this is a live-adversary sourcing gap for THIS scenario
#: against THIS model, not a broken guardrail. Named explicitly here, and
#: `_is_known_non_exercised_gap` below only swallows the SPECIFIC "never
#: exercised" failure mode, so a real regression -- on this scenario or any
#: other -- still surfaces as a hard, unmistakable pytest failure.
_KNOWN_NON_EXERCISABLE_SCENARIOS = frozenset({"02_layer2_revalidation"})


def _is_known_non_exercised_gap(scenario_name: str, result: ScenarioResult) -> bool:
    """True only when *scenario_name* is one of
    `_KNOWN_NON_EXERCISABLE_SCENARIOS` AND *result*'s gate failed for the
    single, narrow reason that it was never exercised
    (`exercised_count == 0`) -- never for a scenario that DID exercise but
    still broke (a real regression), and never for a scenario not
    explicitly named here.
    """
    return (
        scenario_name in _KNOWN_NON_EXERCISABLE_SCENARIOS
        and not result.gate.passed
        and result.exercised_count == 0
    )


@pytest.mark.live
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

    # PR #102 review fix -- a documented, narrowly-scoped non-exercise gap
    # (see `_KNOWN_NON_EXERCISABLE_SCENARIOS` above) is reported as an
    # expected failure, not a hard one: this is a live-adversary sourcing
    # limitation of one named scenario against one pinned model, honestly
    # disclosed in the PR, never a silent downgrade of the guardrail gate
    # itself -- every OTHER failure (a real break, a crash, or this same
    # scenario failing for any OTHER reason) still hits the strict assert
    # below unchanged.
    if _is_known_non_exercised_gap(scenario.name, result):
        pytest.xfail(
            "documented live-elicitation gap (see the scenario file's own "
            f"header comment): {result.gate.reason}"
        )

    # The actual guardrail gate: held in every exercised run, and exercised
    # at least once (#76/#81's Principle -- a guardrail never tried proves
    # nothing). A broken guardrail is a security finding, not a flaky test
    # -- this assertion is deliberately never weakened; see the PR body for
    # this run's actual held/broken/not-exercised results.
    assert result.gate.passed, result.gate.reason


# ---------------------------------------------------------------------------
# Offline tests (PR #102 review fixes) -- no live model, DB, or sandbox.
# ---------------------------------------------------------------------------


def _fake_result(
    *, scenario: str, runs: tuple[RunOutcome, ...], category: str = CATEGORY_GUARDRAIL
) -> ScenarioResult:
    return ScenarioResult(
        scenario=scenario,
        role="sales-agent",
        model="fake-model",
        runs=runs,
        category=category,
    )


def test_is_known_non_exercised_gap_true_for_the_named_scenario_never_exercised() -> (
    None
):
    scenario_name = next(iter(_KNOWN_NON_EXERCISABLE_SCENARIOS))
    result = _fake_result(
        scenario=scenario_name,
        runs=(RunOutcome(passed=True, exercised=False),) * 3,
    )

    assert result.gate.passed is False  # sanity: this IS the never-exercised gate
    assert _is_known_non_exercised_gap(scenario_name, result) is True


def test_is_known_non_exercised_gap_false_once_the_scenario_actually_exercises() -> (
    None
):
    """If the scenario ever starts exercising (a mechanism or model
    improvement), a genuine break must surface as a hard failure again, not
    be swallowed by this escape hatch."""
    scenario_name = next(iter(_KNOWN_NON_EXERCISABLE_SCENARIOS))
    result = _fake_result(
        scenario=scenario_name,
        runs=(RunOutcome(passed=False, exercised=True),),
    )

    assert result.gate.passed is False  # broke, not merely never-exercised
    assert _is_known_non_exercised_gap(scenario_name, result) is False


def test_is_known_non_exercised_gap_false_for_an_unrelated_never_exercised_scenario() -> (
    None
):
    result = _fake_result(
        scenario="06_tool_call_timeout",
        runs=(RunOutcome(passed=True, exercised=False),) * 3,
    )

    assert _is_known_non_exercised_gap("06_tool_call_timeout", result) is False


def test_is_known_non_exercised_gap_false_when_the_named_scenario_already_passed() -> (
    None
):
    scenario_name = next(iter(_KNOWN_NON_EXERCISABLE_SCENARIOS))
    result = _fake_result(
        scenario=scenario_name,
        runs=(RunOutcome(passed=True, exercised=True),),
    )

    assert result.gate.passed is True
    assert _is_known_non_exercised_gap(scenario_name, result) is False


def test_tool_call_timeout_scenario_discloses_it_only_proves_the_transcript_was_bounded() -> (
    None
):
    """PR #102 review fix (issue #105) -- `06_tool_call_timeout.yaml`'s
    assertions (`tools_called`/`not_executed` on `use_term`) read only the
    harness's reported transcript, never real host process state, so a
    "held" result here cannot by itself prove the underlying sandboxed
    process was actually terminated: nested `asyncio.timeout` scopes do not
    propagate a kill that way when the OUTER (interceptor) timeout is what
    fires, as it does in this scenario (see issue #105). The scenario's own
    header comment must say so explicitly, so a future reader never mistakes
    "held" for "resource-bounded".
    """
    source = (_SCENARIOS_DIR / "06_tool_call_timeout.yaml").read_text(encoding="utf-8")
    # Strip each line's leading "# " comment marker before collapsing
    # whitespace, so a phrase that happens to wrap across two comment lines
    # (like this file's own header) is still matched as one sentence.
    stripped_lines = (line.lstrip("#").strip() for line in source.splitlines())
    normalized = " ".join(stripped_lines).lower()

    assert "does not prove the real sandboxed process was terminated" in normalized
    assert "#105" in source
