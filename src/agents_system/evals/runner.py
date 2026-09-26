"""Live-eval runner: assertion evaluation and N-run success rates (#169,
ADR-002 E.18).

Runs a scenario's role through the SAME `resolve()`/`build_runtime()` path
every other consumer of the platform uses, so a live eval exercises exactly
the production role-resolution and Layer-1/Layer-2 tool-interception
pipeline -- never a shortcut built only for evaluation.

Every assertion is behavioral: which tool the model called, whether a call
was denied, whether an escalation succeeded. None of them compare the
model's generated text -- ADR-002 E.18 is explicit that exact-text
assertions are not meaningful or stable against a real, non-deterministic
model.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable, Sequence
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage

from agents_system.agent.graph import AgentRuntime, TurnUsage
from agents_system.audit.sink import AuditSink
from agents_system.evals.schema import Scenario, ScenarioAssertions
from agents_system.harness.factory import EquippedRuntime, build_runtime
from agents_system.harness.loader import RootConfig, resolve
from agents_system.harness.registry import ToolRegistry

#: The exact prefix `agent/graph.py::_execute_tools` writes into a
#: `ToolMessage.content` when the Layer-2 interceptor raises `PolicyViolation`
#: (reasons: not_in_surface / revalidation_required / permission_revoked).
#: A timeout also sets `status="error"` but with different content, so
#: matching this prefix -- not just the status -- is what tells a genuine
#: permission denial apart from any other tool-call failure.
_BLOCKED_PREFIX = "Tool call blocked:"

#: `connectors/platform_connectors.py::build_escalation_notifier_tool_spec`
#: registers this exact tool name; no shared constant exists there to import
#: (it is a one-off literal at that single construction site), so this is
#: its own local copy for the two `escalation_expected` checks below.
_ESCALATION_NOTIFIER_TOOL = "escalation_notifier"

#: Compatibility policy applied when a scenario omits ``granted_permissions``.
#: Kept named and logged so an implicit YAML omission remains observable.
_ALL_DECLARED_GRANT_POLICY = "all-declared"

logger = structlog.get_logger()


@dataclasses.dataclass(frozen=True)
class AssertionFailure:
    """One assertion that did not hold for a run's transcript."""

    kind: str
    detail: str


@dataclasses.dataclass(frozen=True)
class AssertionOutcome:
    """The result of checking one transcript against a scenario's assertions."""

    failures: tuple[AssertionFailure, ...]

    @property
    def passed(self) -> bool:
        return not self.failures


@dataclasses.dataclass(frozen=True)
class RunOutcome:
    """The outcome of one execution of a scenario.

    `error` is set only when the run itself raised (a real infrastructure
    failure -- the model call errored, the graph timed out unhandled, etc.),
    as distinct from `failures`, which are assertions that were evaluated and
    did not hold. Both count as a failed run for the success rate.
    """

    passed: bool
    failures: tuple[AssertionFailure, ...] = ()
    error: str | None = None
    #: #78 Phase 0 -- this run's real token usage/cost, summed across every
    #: turn `scenario.turns` made (`_sum_turn_usage` below). `None` when the
    #: run raised before any turn returned (see `error` above).
    usage: TurnUsage | None = None


def _sum_turn_usage(turns: Sequence[TurnUsage]) -> TurnUsage:
    """Sum several `TurnUsage` values -- a run's turns, or a scenario's runs
    -- into one total (#78 Phase 0), applying `TurnUsage`'s own honesty rule
    at this coarser grain too: one unknown field in any input makes that
    field unknown for the whole sum, rather than silently under-reporting.
    """
    if any(turn.model_calls is None for turn in turns):
        model_calls = None
    else:
        model_calls = sum(turn.model_calls for turn in turns)  # type: ignore[misc]
    if any(turn.total_tokens is None for turn in turns):
        input_tokens = output_tokens = total_tokens = None
    else:
        input_tokens = sum(turn.input_tokens for turn in turns)  # type: ignore[misc]
        output_tokens = sum(turn.output_tokens for turn in turns)  # type: ignore[misc]
        total_tokens = sum(turn.total_tokens for turn in turns)  # type: ignore[misc]
    if any(turn.cost_usd is None for turn in turns):
        cost_usd = None
    else:
        cost_usd = sum(turn.cost_usd for turn in turns)  # type: ignore[misc]
    return TurnUsage(
        model_calls=model_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
    )


@dataclasses.dataclass(frozen=True)
class ScenarioResult:
    """A scenario's outcomes across `runs` executions, against one model."""

    scenario: str
    role: str
    model: str
    runs: tuple[RunOutcome, ...]
    #: How many audit events (ToolCallAttempted, ToolCallBlocked, ...) the
    #: run's own AuditSink captured. Diagnostic only -- assertions never
    #: depend on it, see `evaluate_assertions`'s docstring note below.
    audit_events_captured: int = 0

    @property
    def success_rate(self) -> float:
        if not self.runs:
            return 0.0
        return sum(1 for run in self.runs if run.passed) / len(self.runs)

    @property
    def total_usage(self) -> TurnUsage | None:
        """This scenario's real token usage/cost, summed across every run
        (#78 Phase 0) -- `None` only when there are no runs at all.

        Review finding 2 (PR #87): if even ONE run's usage is unknown
        (`RunOutcome.usage is None` -- a run that raised before completing a
        turn), the WHOLE scenario's total is honestly unknown too, applying
        `TurnUsage`'s own honesty rule at this coarser grain. The previous
        implementation filtered unknown runs out and summed only the known
        ones, which for a flaky scenario (some runs pass, one crashes)
        silently under-reported a partial sum as if it were the complete
        total -- `write_results`'s markdown table would show a real-looking
        number instead of `n/a`.
        """
        if not self.runs:
            return None
        if any(run.usage is None for run in self.runs):
            return TurnUsage(
                model_calls=None,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cost_usd=None,
            )
        return _sum_turn_usage(
            [run.usage for run in self.runs if run.usage is not None]
        )

    def to_dict(self) -> dict[str, Any]:
        total_usage = self.total_usage
        return {
            "scenario": self.scenario,
            "role": self.role,
            "model": self.model,
            "runs": len(self.runs),
            "passed": sum(1 for run in self.runs if run.passed),
            "success_rate": self.success_rate,
            "audit_events_captured": self.audit_events_captured,
            "total_tokens": total_usage.total_tokens if total_usage else None,
            "total_cost_usd": total_usage.cost_usd if total_usage else None,
            "run_details": [
                {
                    "run": index,
                    "passed": run.passed,
                    "error": run.error,
                    "failures": [dataclasses.asdict(f) for f in run.failures],
                    "total_tokens": run.usage.total_tokens if run.usage else None,
                    "cost_usd": run.usage.cost_usd if run.usage else None,
                }
                for index, run in enumerate(self.runs)
            ],
        }


def _called_tool_names(messages: Sequence[AnyMessage]) -> frozenset[str]:
    """Tool names the model attempted to call, regardless of the outcome."""
    return frozenset(
        call["name"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    )


def _succeeded_tool_names(messages: Sequence[AnyMessage]) -> frozenset[str]:
    """Tool names whose call executed with no interceptor denial, no
    timeout, and no connector-reported `error_kind` -- the behavioral
    definition of "the call actually worked", never a comparison against
    generated text.
    """
    call_names_by_id: dict[str, str] = {
        call["id"]: call["name"]
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
        if call["id"] is not None
    }

    succeeded: set[str] = set()
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        name = call_names_by_id.get(message.tool_call_id)
        if name is None or message.status == "error":
            continue
        content = message.content
        if isinstance(content, str):
            try:
                parsed: Any = json.loads(content)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict) and "error_kind" in parsed:
                continue
        succeeded.add(name)
    return frozenset(succeeded)


def _was_denied(messages: Sequence[AnyMessage]) -> bool:
    return any(
        isinstance(message, ToolMessage)
        and message.status == "error"
        and isinstance(message.content, str)
        and message.content.startswith(_BLOCKED_PREFIX)
        for message in messages
    )


def evaluate_assertions(
    assertions: ScenarioAssertions, messages: Sequence[AnyMessage]
) -> AssertionOutcome:
    """Check *messages* (one run's full accumulated transcript) against
    *assertions*. Every check is behavioral -- never text equality.

    `permission_denied` reads the synchronous `ToolMessage` the interceptor
    already returns (`_was_denied` above), not the run's `AuditSink` (see
    `run_scenario`): the sink's own drainer batches on a 100ms timer, so a
    denial recorded there is only eventually consistent by the time a run
    finishes, while the `ToolMessage` is available the instant `run_turn`
    returns. The sink stays for delivering real audit events during a live
    eval (they were previously silently dropped) and for diagnostics, not as
    an assertion source.
    """
    failures: list[AssertionFailure] = []
    called = _called_tool_names(messages)
    succeeded = _succeeded_tool_names(messages)

    for tool in assertions.tools_called:
        if tool not in called:
            failures.append(
                AssertionFailure(
                    "tools_called", f"expected a call to {tool!r}; it was not called"
                )
            )

    for tool in assertions.tools_not_called:
        if tool in called:
            failures.append(
                AssertionFailure(
                    "tools_not_called", f"{tool!r} must not be called, but it was"
                )
            )

    if assertions.permission_denied is not None:
        denied = _was_denied(messages)
        if denied != assertions.permission_denied:
            failures.append(
                AssertionFailure(
                    "permission_denied",
                    f"expected permission_denied={assertions.permission_denied}, "
                    f"got {denied}",
                )
            )

    if (
        assertions.escalation_expected is True
        and _ESCALATION_NOTIFIER_TOOL not in succeeded
    ):
        failures.append(
            AssertionFailure(
                "escalation_expected",
                "expected a successful escalation_notifier call; none found",
            )
        )
    elif (
        assertions.escalation_expected is False and _ESCALATION_NOTIFIER_TOOL in called
    ):
        failures.append(
            AssertionFailure(
                "escalation_expected",
                "escalation_notifier must not be called, but it was",
            )
        )

    return AssertionOutcome(failures=tuple(failures))


class _CapturingAuditSink(AuditSink):
    """A real `AuditSink` whose persistence is an in-memory list instead of
    a database write -- same shape as `tests/test_audit_wiring.py`'s own
    `CapturingSink`.

    A live-eval run has no FastAPI lifespan to construct the app's real,
    DB-backed sink. Without ANY sink registered, every audit-emitting call
    along the way (`interceptor.intercept`, `injector.resolve_tool_surface`,
    ...) hits `AuditSink.current()`'s `RuntimeError` -- silently swallowed by
    `injector._emit`, so the failure mode was not a crash, it was every
    audit event for every eval run being quietly dropped. `run_scenario`
    registers one of these for the run's duration instead.

    `session_factory=None` is safe here because `_flush_batch` is overridden
    below and never touches it -- the real `AuditSink` only reads it to open
    a SQLAlchemy session, which this sink never does.
    """

    def __init__(self) -> None:
        super().__init__(session_factory=None, maxsize=1000)  # type: ignore[arg-type]
        self.captured: list[Any] = []

    async def _flush_batch(self, batch: list[Any]) -> None:
        self.captured.extend(batch)


async def _settle_audit_sink(
    sink: _CapturingAuditSink, *, attempts: int = 20, interval_s: float = 0.02
) -> None:
    """Give the sink's background drainer (100ms batch window) a chance to
    flush queued events into `captured` before the caller inspects it --
    mirrors `tests/test_audit_wiring.py`'s own `_settle` helper.
    """
    for _ in range(attempts):
        if sink.captured:
            return
        await asyncio.sleep(interval_s)


async def run_scenario(
    scenario: Scenario,
    *,
    model: BaseChatModel,
    model_name: str,
    registry: ToolRegistry | None = None,
    registry_factory: Callable[[], ToolRegistry] | None = None,
    roots: RootConfig | None = None,
    runs: int = 1,
    audit_sink_factory: Callable[[], _CapturingAuditSink] = _CapturingAuditSink,
) -> ScenarioResult:
    """Run *scenario* `runs` times against `model`, returning a per-run
    outcome and an aggregate success rate.

    Resolves the role through the same `resolve()`/`build_runtime()` path
    every consumer of the platform uses, then drives it through
    `AgentRuntime.run_turn` -- one call per scenario turn, feeding each run's
    accumulated history back in, exactly the shape a real caller uses. A run
    that raises (infrastructure failure, not an unmet assertion) is recorded
    as a failed run with `error` set, rather than propagating and aborting
    every remaining run.

    Exactly one of `registry` / `registry_factory` must be given (#171):

    - `registry`: the original behavior. One `ToolRegistry`, and one
      `EquippedRuntime` built from it up front, reused for every one of
      `runs` runs. Correct for a stateless registry -- wrong for one backed
      by process-local mutable state (e.g. `ReferenceBackends`'s message
      ledger, PR #176 review note 1), where reusing it across runs leaks
      that state between them.
    - `registry_factory`: called once PER RUN, and `build_runtime` re-run
      against each fresh registry it returns, so every run gets its own,
      unshared backend instances.

    An explicit ``scenario.granted_permissions`` wire-name list is passed
    unmodified to ``build_runtime``. When omitted, the runner applies and
    logs the named ``all-declared`` compatibility policy, resolving the role
    once to pass its declared permissions to ``build_runtime``. In either
    case, ``build_runtime`` derives the Layer-1 surface and Layer-2 deploy
    grant ceiling from that same grant under R3/R4 enforcement.

    `audit_sink_factory` builds the `AuditSink` registered for the whole
    call (default: an in-memory `_CapturingAuditSink`) so the platform's
    real audit wiring actually delivers during an eval instead of every
    event being dropped (see `_CapturingAuditSink`'s docstring). Whatever
    sink was registered before this call, if any, is restored afterward;
    injectable for tests that want their own handle on what was captured.
    """
    if (registry is None) == (registry_factory is None):
        raise ValueError(
            "run_scenario requires exactly one of `registry` or "
            "`registry_factory`, not both and not neither."
        )

    roots = roots if roots is not None else RootConfig()

    # Registered BEFORE resolve()/build_runtime() below, not just before the
    # run loop: build_runtime() itself emits a `record_runtime_built` audit
    # event (`harness/factory.py`), and that call happens before this
    # function's own run loop ever starts. Registering the sink any later
    # left exactly that one event hitting AuditSink.current()'s RuntimeError
    # same as before this fix -- swallowed by injector._emit, but still lost.
    sink = audit_sink_factory()
    await sink.start()
    try:
        previous_sink: AuditSink | None = AuditSink.current()
    except RuntimeError:
        previous_sink = None
    AuditSink.set_current(sink)

    try:
        if scenario.granted_permissions is not None:
            granted_permissions = scenario.granted_permissions
        else:
            # `all-declared` preserves existing YAML files that predate
            # explicit grants. It is named and logged rather than silently
            # widening: build_runtime() still applies R3/R4 and persists the
            # same bounded grant ceiling for Layer 2.
            granted_permissions = resolve(
                scenario.role, client=scenario.client, roots=roots
            ).permissions
            logger.info(
                "eval.grants_defaulted",
                policy=_ALL_DECLARED_GRANT_POLICY,
                scenario=scenario.name,
                role=scenario.role,
            )

        def _build_equipped(active_registry: ToolRegistry) -> EquippedRuntime:
            return build_runtime(
                scenario.role,
                active_registry,
                granted_permissions,
                client=scenario.client,
                roots=roots,
            )

        # `registry`: build ONE EquippedRuntime up front, reused below for
        # every run (original behavior). `registry_factory`: leave it unset
        # here and rebuild fresh, once per run, inside the loop.
        equipped = _build_equipped(registry) if registry is not None else None

        outcomes: list[RunOutcome] = []
        for index in range(runs):
            if registry_factory is not None:
                equipped = _build_equipped(registry_factory())
            assert equipped is not None  # exactly one branch above set it
            agent = AgentRuntime(equipped, model)
            history: list[AnyMessage] = []
            # #78 Phase 0 (review finding 5) -- one TurnUsage per turn this
            # run makes; summed into the RunOutcome below.
            # `run_turn_with_usage` (not `run_turn`) is the explicit,
            # type-safe way to get `.usage` -- see `TurnResult`'s docstring
            # for why this replaced the earlier `TurnMessages` list subclass.
            turn_usages: list[TurnUsage] = []
            try:
                for turn in scenario.turns:
                    history = [*history, HumanMessage(content=turn)]
                    turn_result = await agent.run_turn_with_usage(
                        history,
                        session_id=f"eval-{scenario.name}-{index}",
                        # Prices this run's usage under the same id
                        # ScenarioResult.model already reports.
                        model_id=model_name,
                    )
                    turn_usages.append(turn_result.usage)
                    history = turn_result.messages
                outcome = evaluate_assertions(scenario.assertions, history)
                outcomes.append(
                    RunOutcome(
                        passed=outcome.passed,
                        failures=outcome.failures,
                        usage=_sum_turn_usage(turn_usages) if turn_usages else None,
                    )
                )
            except Exception as exc:
                # a failed run, not a crashed eval: one bad run must not
                # abort every remaining one, or a single flaky call would
                # silently erase the rest of the success-rate signal.
                outcomes.append(RunOutcome(passed=False, error=str(exc)))
    finally:
        await _settle_audit_sink(sink)
        await sink.stop()
        if previous_sink is not None:
            AuditSink.set_current(previous_sink)

    return ScenarioResult(
        scenario=scenario.name,
        role=scenario.role,
        model=model_name,
        runs=tuple(outcomes),
        audit_events_captured=len(sink.captured),
    )
