"""Offline tests for the live-eval runner (#169, ADR-002 E.18).

`evaluate_assertions` is tested directly against hand-built transcripts.
`run_scenario` is tested through the REAL `resolve()` / `build_runtime()` /
`AgentRuntime` pipeline -- the same shape `tests/test_agent_runtime.py`
already uses -- with only the model faked, so these tests prove the runner's
own logic (rate aggregation, a failing assertion counted as a failed run)
without any network call. The `live`-marked test in
`tests/test_live_eval_sales_agent.py` is what actually calls Ollama.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from unittest.mock import patch

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage

import agents_system.evals.runner as runner_module
from agents_system.audit.sink import AuditSink
from agents_system.evals.runner import (
    _CapturingAuditSink,
    evaluate_assertions,
    run_scenario,
)
from agents_system.evals.schema import Scenario, ScenarioAssertions
from agents_system.harness.loader import RootConfig


class ToolAwareFakeModel(FakeMessagesListChatModel):
    """Same fake-model shape as `test_agent_runtime.py` -- `bind_tools` must
    not raise, since `AgentRuntime.__init__` calls it whenever the role has
    any tools."""

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> ToolAwareFakeModel:
        return self


def _tool_call(
    name: str, args: dict[str, Any] | None = None, call_id: str = "call-1"
) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"id": call_id, "name": name, "args": args or {}, "type": "tool_call"}
        ],
    )


def _scenario(**assertion_kwargs: Any) -> Scenario:
    return Scenario(
        name="unit-test-scenario",
        role="sales-agent",
        turns=("Do you have Item Alpha in stock?",),
        assertions=ScenarioAssertions(**assertion_kwargs),
    )


# ---------------------------------------------------------------------------
# evaluate_assertions
# ---------------------------------------------------------------------------


def test_evaluate_assertions_passes_when_an_expected_tool_was_called() -> None:
    messages = [
        _tool_call("catalog_search", {"q": "alpha"}),
        ToolMessage(content='{"results": []}', tool_call_id="call-1"),
    ]

    outcome = evaluate_assertions(
        ScenarioAssertions(tools_called=("catalog_search",)), messages
    )

    assert outcome.passed
    assert outcome.failures == ()


def test_evaluate_assertions_fails_when_an_expected_tool_was_not_called() -> None:
    messages = [AIMessage(content="Sure, let me help.")]

    outcome = evaluate_assertions(
        ScenarioAssertions(tools_called=("catalog_search",)), messages
    )

    assert not outcome.passed
    assert outcome.failures[0].kind == "tools_called"


def test_evaluate_assertions_fails_when_a_forbidden_tool_was_called() -> None:
    messages = [
        _tool_call("order_writer", {"client_id": "c1", "items": []}),
        ToolMessage(content='{"order_id": "o1"}', tool_call_id="call-1"),
    ]

    outcome = evaluate_assertions(
        ScenarioAssertions(tools_not_called=("order_writer",)), messages
    )

    assert not outcome.passed
    assert outcome.failures[0].kind == "tools_not_called"


def test_evaluate_assertions_permission_denied_true_matches_an_interceptor_block() -> (
    None
):
    messages = [
        _tool_call("order_writer"),
        ToolMessage(
            content="Tool call blocked: not_in_surface",
            tool_call_id="call-1",
            status="error",
        ),
    ]

    outcome = evaluate_assertions(ScenarioAssertions(permission_denied=True), messages)

    assert outcome.passed


def test_evaluate_assertions_permission_denied_true_fails_when_nothing_was_blocked() -> (
    None
):
    messages = [AIMessage(content="All good.")]

    outcome = evaluate_assertions(ScenarioAssertions(permission_denied=True), messages)

    assert not outcome.passed
    assert outcome.failures[0].kind == "permission_denied"


def test_evaluate_assertions_permission_denied_false_fails_on_a_block() -> None:
    messages = [
        _tool_call("order_writer"),
        ToolMessage(
            content="Tool call blocked: not_in_surface",
            tool_call_id="call-1",
            status="error",
        ),
    ]

    outcome = evaluate_assertions(ScenarioAssertions(permission_denied=False), messages)

    assert not outcome.passed


def test_evaluate_assertions_a_timeout_is_not_mistaken_for_a_permission_denial() -> (
    None
):
    """`_execute_tools` also sets status="error" on a timeout, with different
    content -- only the interceptor's own "Tool call blocked:" prefix counts
    as a permission denial."""
    messages = [
        _tool_call("catalog_search"),
        ToolMessage(
            content="Tool call timed out after 5.0s",
            tool_call_id="call-1",
            status="error",
        ),
    ]

    outcome = evaluate_assertions(ScenarioAssertions(permission_denied=True), messages)

    assert not outcome.passed


def test_evaluate_assertions_escalation_expected_true_requires_a_successful_call() -> (
    None
):
    messages = [
        _tool_call("escalation_notifier", {"reason": "r", "details": "d"}),
        ToolMessage(
            content='{"escalation_id": "e1", "status": "logged"}',
            tool_call_id="call-1",
        ),
    ]

    outcome = evaluate_assertions(
        ScenarioAssertions(escalation_expected=True), messages
    )

    assert outcome.passed


def test_evaluate_assertions_escalation_expected_true_fails_on_a_failed_call() -> None:
    messages = [
        _tool_call("escalation_notifier", {"reason": "r", "details": "d"}),
        ToolMessage(
            content='{"error": "not configured", "error_kind": "escalation_not_configured"}',
            tool_call_id="call-1",
        ),
    ]

    outcome = evaluate_assertions(
        ScenarioAssertions(escalation_expected=True), messages
    )

    assert not outcome.passed
    assert outcome.failures[0].kind == "escalation_expected"


def test_evaluate_assertions_escalation_expected_false_fails_even_on_a_failed_attempt() -> (
    None
):
    """Forbidding escalation means the model must not even ATTEMPT the call
    -- a failed attempt still shows the model chose to escalate."""
    messages = [
        _tool_call("escalation_notifier", {"reason": "r", "details": "d"}),
        ToolMessage(
            content='{"error_kind": "escalation_not_configured"}', tool_call_id="call-1"
        ),
    ]

    outcome = evaluate_assertions(
        ScenarioAssertions(escalation_expected=False), messages
    )

    assert not outcome.passed


def test_evaluate_assertions_escalation_expected_true_treats_non_json_content_as_success() -> (
    None
):
    """A tool result whose content isn't JSON still counts as a successful
    call -- only an explicit `error_kind` in a parsed JSON object marks
    failure; a plain-text result is not itself evidence of one."""
    messages = [
        _tool_call("escalation_notifier", {"reason": "r", "details": "d"}),
        ToolMessage(content="ok", tool_call_id="call-1"),
    ]

    outcome = evaluate_assertions(
        ScenarioAssertions(escalation_expected=True), messages
    )

    assert outcome.passed


def test_evaluate_assertions_with_no_assertions_always_passes() -> None:
    outcome = evaluate_assertions(ScenarioAssertions(), [AIMessage(content="hi")])

    assert outcome.passed


# ---------------------------------------------------------------------------
# run_scenario -- real resolve()/build_runtime()/AgentRuntime pipeline,
# fake model only.
# ---------------------------------------------------------------------------


async def test_run_scenario_reports_a_full_success_rate_when_every_run_passes() -> None:
    from conftest import build_test_registry

    model = ToolAwareFakeModel(
        responses=[
            _tool_call("catalog_search", {"q": "Item Alpha"}),
            AIMessage(content="Yes, Item Alpha is in stock."),
        ]
    )

    result = await run_scenario(
        _scenario(tools_called=("catalog_search",)),
        model=model,
        model_name="fake-model",
        registry=build_test_registry(),
        roots=RootConfig(),
        runs=1,
    )

    assert result.scenario == "unit-test-scenario"
    assert result.role == "sales-agent"
    assert result.model == "fake-model"
    assert len(result.runs) == 1
    assert result.runs[0].passed
    assert result.success_rate == 1.0


async def test_run_scenario_a_failing_assertion_is_counted_as_a_failed_run() -> None:
    from conftest import build_test_registry

    # The fake model never calls a tool -- the tools_called assertion must fail.
    model = ToolAwareFakeModel(responses=[AIMessage(content="Sure, one moment.")])

    result = await run_scenario(
        _scenario(tools_called=("catalog_search",)),
        model=model,
        model_name="fake-model",
        registry=build_test_registry(),
        roots=RootConfig(),
        runs=1,
    )

    assert result.success_rate == 0.0
    assert not result.runs[0].passed
    assert result.runs[0].failures[0].kind == "tools_called"
    assert result.runs[0].error is None


async def test_run_scenario_aggregates_a_success_rate_across_multiple_runs() -> None:
    from conftest import build_test_registry

    no_call_response = AIMessage(content="Sorry, I don't know.")
    call_response = _tool_call("catalog_search", {"q": "Item Alpha"})
    final_response = AIMessage(content="Here you go.")

    # FakeMessagesListChatModel steps through `responses` once per model
    # invocation: run 1 fails after its single call (no tool_calls -> the
    # graph ends immediately); run 2's two calls (tool call, then final text)
    # consume the remaining two responses.
    model = ToolAwareFakeModel(
        responses=[no_call_response, call_response, final_response]
    )

    result = await run_scenario(
        _scenario(tools_called=("catalog_search",)),
        model=model,
        model_name="fake-model",
        registry=build_test_registry(),
        roots=RootConfig(),
        runs=2,
    )

    assert len(result.runs) == 2
    assert result.runs[0].passed is False
    assert result.runs[1].passed is True
    assert result.success_rate == 0.5


def test_scenario_result_success_rate_is_zero_with_no_runs() -> None:
    from agents_system.evals.runner import ScenarioResult

    result = ScenarioResult(
        scenario="s", role="sales-agent", model="fake-model", runs=()
    )

    assert result.success_rate == 0.0


async def test_run_scenario_records_a_crashed_run_as_failed_with_its_error() -> None:
    """A run that raises (an infrastructure failure, not an unmet assertion)
    is recorded as a failed run with `error` set -- and does not abort the
    remaining runs."""
    from conftest import build_test_registry

    class _ExplodingModel(ToolAwareFakeModel):
        def _generate(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated model failure")

    model = _ExplodingModel(responses=[AIMessage(content="unreachable")])

    result = await run_scenario(
        _scenario(tools_called=("catalog_search",)),
        model=model,
        model_name="fake-model",
        registry=build_test_registry(),
        roots=RootConfig(),
        runs=1,
    )

    assert len(result.runs) == 1
    assert result.runs[0].passed is False
    assert result.runs[0].error == "simulated model failure"
    assert result.runs[0].failures == ()


async def test_run_scenario_to_dict_carries_role_model_and_rate() -> None:
    from conftest import build_test_registry

    model = ToolAwareFakeModel(responses=[AIMessage(content="hi")])

    result = await run_scenario(
        _scenario(),
        model=model,
        model_name="fake-model",
        registry=build_test_registry(),
        roots=RootConfig(),
        runs=1,
    )

    payload = result.to_dict()

    assert payload["scenario"] == "unit-test-scenario"
    assert payload["role"] == "sales-agent"
    assert payload["model"] == "fake-model"
    assert payload["runs"] == 1
    assert payload["success_rate"] == 1.0


# ---------------------------------------------------------------------------
# run_scenario -- redundant resolve() (review nit)
# ---------------------------------------------------------------------------


async def test_run_scenario_skips_its_own_resolve_when_granted_permissions_is_given() -> (
    None
):
    """A scenario that names its own granted_permissions no longer pays for
    the extra resolve() call that used to run unconditionally just to read
    definition.permissions for the default grant -- build_runtime() still
    resolves the role internally regardless, this only removes runner.py's
    OWN redundant call."""
    from conftest import build_test_registry

    scenario = Scenario(
        name="explicit-grant",
        role="sales-agent",
        turns=("hi",),
        assertions=ScenarioAssertions(),
        granted_permissions=("read:catalog",),
    )
    model = ToolAwareFakeModel(responses=[AIMessage(content="hi")])

    with patch.object(
        runner_module, "resolve", wraps=runner_module.resolve
    ) as resolve_spy:
        await run_scenario(
            scenario,
            model=model,
            model_name="fake-model",
            registry=build_test_registry(),
            roots=RootConfig(),
            runs=1,
        )

    resolve_spy.assert_not_called()


async def test_run_scenario_still_resolves_once_when_granted_permissions_is_default() -> (
    None
):
    """The "grant everything the role declares" default still needs exactly
    one resolve() call from runner.py itself -- there is no other way to
    learn definition.permissions before build_runtime() can be called with
    it."""
    from conftest import build_test_registry

    model = ToolAwareFakeModel(
        responses=[
            _tool_call("catalog_search", {"q": "Item Alpha"}),
            AIMessage(content="Yes, in stock."),
        ]
    )

    with patch.object(
        runner_module, "resolve", wraps=runner_module.resolve
    ) as resolve_spy:
        await run_scenario(
            _scenario(tools_called=("catalog_search",)),
            model=model,
            model_name="fake-model",
            registry=build_test_registry(),
            roots=RootConfig(),
            runs=1,
        )

    resolve_spy.assert_called_once()


# ---------------------------------------------------------------------------
# run_scenario -- AuditSink registration
# ---------------------------------------------------------------------------


async def test_run_scenario_registers_an_audit_sink_and_captures_events() -> None:
    """Before this fix, every eval turn's audit-emitting calls
    (interceptor.intercept, injector.resolve_tool_surface, ...) silently hit
    `AuditSink.current()`'s RuntimeError -- swallowed by injector._emit, so
    every audit event for every eval run was quietly dropped. run_scenario
    must register its own sink for the run so those events actually land."""
    from conftest import build_test_registry

    captured_sink = _CapturingAuditSink()

    model = ToolAwareFakeModel(
        responses=[
            _tool_call("catalog_search", {"q": "Item Alpha"}),
            AIMessage(content="Yes, in stock."),
        ]
    )

    result = await run_scenario(
        _scenario(tools_called=("catalog_search",)),
        model=model,
        model_name="fake-model",
        registry=build_test_registry(),
        roots=RootConfig(),
        runs=1,
        audit_sink_factory=lambda: captured_sink,
    )

    assert result.runs[0].passed
    assert captured_sink.captured, "no audit events were captured for the run"
    assert result.audit_events_captured == len(captured_sink.captured)
    # build_runtime() itself emits a runtime_built event BEFORE the run
    # loop starts -- proving it landed catches the sink being registered
    # only late enough to see per-call events but not this one (the exact
    # regression a live OpenRouter run caught: this event still raised the
    # swallowed RuntimeError when the sink was registered after
    # build_runtime() instead of before it).
    captured_event_types = {
        getattr(event, "event_type", None) for event in captured_sink.captured
    }
    assert "runtime_built" in captured_event_types


async def test_run_scenario_restores_the_previously_registered_audit_sink() -> None:
    """A caller that already has its own AuditSink registered (e.g. a real
    app process embedding an eval run) must get it back afterward -- the
    eval's own sink must not leak as the new ambient default."""
    from conftest import build_test_registry

    previous = _CapturingAuditSink()
    await previous.start()
    AuditSink.set_current(previous)
    try:
        model = ToolAwareFakeModel(responses=[AIMessage(content="hi")])

        await run_scenario(
            _scenario(),
            model=model,
            model_name="fake-model",
            registry=build_test_registry(),
            roots=RootConfig(),
            runs=1,
        )

        assert AuditSink.current() is previous
    finally:
        await previous.stop()


# ---------------------------------------------------------------------------
# run_scenario -- registry_factory (#171): a fresh registry per run, for a
# stateful backend that must not leak state across runs (PR #176 review note
# 1: ReferenceBackends keeps a per-instance message ledger).
# ---------------------------------------------------------------------------


async def test_run_scenario_requires_exactly_one_of_registry_or_registry_factory() -> (
    None
):
    from conftest import build_test_registry

    model = ToolAwareFakeModel(responses=[AIMessage(content="hi")])

    with pytest.raises(ValueError):
        await run_scenario(
            _scenario(),
            model=model,
            model_name="fake-model",
            roots=RootConfig(),
            runs=1,
        )

    with pytest.raises(ValueError):
        await run_scenario(
            _scenario(),
            model=model,
            model_name="fake-model",
            registry=build_test_registry(),
            registry_factory=build_test_registry,
            roots=RootConfig(),
            runs=1,
        )


async def test_run_scenario_registry_factory_is_called_once_per_run() -> None:
    from conftest import build_test_registry

    factory_calls: list[int] = []

    def factory() -> Any:
        factory_calls.append(len(factory_calls))
        return build_test_registry()

    model = ToolAwareFakeModel(
        responses=[AIMessage(content="hi")] * 3,
    )

    result = await run_scenario(
        _scenario(),
        model=model,
        model_name="fake-model",
        registry_factory=factory,
        roots=RootConfig(),
        runs=3,
    )

    assert len(result.runs) == 3
    assert len(factory_calls) == 3


async def test_run_scenario_registry_factory_gives_each_run_an_unshared_backend() -> (
    None
):
    """A stateful connector closed over its OWN counter (rebuilt fresh by
    `factory` on every call) must observe count == 1 on every run -- if
    `run_scenario` reused one registry across runs instead of rebuilding it
    per run, the count would instead climb 1, 2, 3, proving state leaked."""
    from conftest import build_test_registry

    observed_counts: list[int] = []

    def factory() -> Any:
        state = {"count": 0}

        def stateful_catalog_search(inputs: dict[str, Any]) -> dict[str, Any]:
            state["count"] += 1
            observed_counts.append(state["count"])
            return {"results": []}

        return build_test_registry(catalog_connector=stateful_catalog_search)

    model = ToolAwareFakeModel(
        responses=[
            _tool_call("catalog_search", {"q": "Item Alpha"}),
            AIMessage(content="ok"),
        ]
        * 3
    )

    result = await run_scenario(
        _scenario(tools_called=("catalog_search",)),
        model=model,
        model_name="fake-model",
        registry_factory=factory,
        roots=RootConfig(),
        runs=3,
    )

    assert len(result.runs) == 3
    assert observed_counts == [1, 1, 1]


async def test_run_scenario_registry_only_behavior_is_unchanged() -> None:
    """The original `registry=` path still builds exactly ONE EquippedRuntime
    up front and reuses it for every run -- proven here the same way state
    leakage is proven above: a stateful connector's counter climbs across
    runs when ONE registry (and therefore one closure) is shared."""
    from conftest import build_test_registry

    state = {"count": 0}
    observed_counts: list[int] = []

    def stateful_catalog_search(inputs: dict[str, Any]) -> dict[str, Any]:
        state["count"] += 1
        observed_counts.append(state["count"])
        return {"results": []}

    model = ToolAwareFakeModel(
        responses=[
            _tool_call("catalog_search", {"q": "Item Alpha"}),
            AIMessage(content="ok"),
        ]
        * 3
    )

    result = await run_scenario(
        _scenario(tools_called=("catalog_search",)),
        model=model,
        model_name="fake-model",
        registry=build_test_registry(catalog_connector=stateful_catalog_search),
        roots=RootConfig(),
        runs=3,
    )

    assert len(result.runs) == 3
    assert observed_counts == [1, 2, 3]
