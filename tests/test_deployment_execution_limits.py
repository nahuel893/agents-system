"""#278 -- execution limits the deployment raises, within the outbox lease,
and a configurable timeout reply.

The platform defaults (60 s per turn, 10 s per tool call, 20 tool calls) are
the ceiling an agent definition is held to. A deployment may set its own
ceiling per runtime (``build_runtime(execution_limits=...)``,
``AgentRuntime(execution_limits=...)``, ``create_app(execution_limits=...)``
or ``EXECUTION_LIMITS``); the agent may still tighten it, never exceed it.

The slow turns here run on an event loop whose clock jumps to the next timer
whenever the loop is idle: ``asyncio.sleep`` and ``asyncio.timeout`` both read
``loop.time()``, so a 105-second turn takes milliseconds and every deadline
still fires in order.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Sequence
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import structlog
from conftest import create_test_app
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatResult
from test_main import _make_settings, _runtime_lease_invariant_patches, _stack

import agents_system
from agents_system.agent.graph import _effective_limits
from agents_system.execution_limits import (
    DEFAULT_TIMEOUT_REPLY,
    LEASE_MARGIN_S,
    OUTBOX_LEASE_S,
    max_turn_timeout_s,
)
from agents_system.harness.loader import DefinitionError
from agents_system.main import lifespan


class _VirtualClockLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock jumps to the next timer when it is idle.

    Idle means nothing is ready and no executor job is running: a thread the
    loop waits on (LangChain runs some callbacks in one) is real work, so the
    clock holds still until it reports back.
    """

    def __init__(self) -> None:
        super().__init__()
        self._now = 0.0
        self._busy = 0
        selector = self._selector  # type: ignore[attr-defined]
        real_select = selector.select

        def select(timeout: float | None = None) -> Any:
            if self._busy:
                return real_select(None if timeout is None else min(timeout, 1.0))
            events = real_select(0)
            if not events and timeout:
                self._now += timeout
            return events

        selector.select = select

    def time(self) -> float:
        return self._now

    def run_in_executor(self, executor: Any, func: Any, *args: Any) -> Any:
        future = super().run_in_executor(executor, func, *args)
        self._busy += 1
        future.add_done_callback(self._job_done)
        return future

    def _job_done(self, _future: Any) -> None:
        self._busy -= 1

    async def shutdown_default_executor(self, timeout: float | None = None) -> None:
        self._busy += 1
        try:
            await super().shutdown_default_executor(timeout)
        finally:
            self._busy -= 1


def _run(coro: Coroutine[Any, Any, Any]) -> Any:
    with asyncio.Runner(loop_factory=_VirtualClockLoop) as runner:
        return runner.run(coro)


class SlowScriptedModel(FakeMessagesListChatModel):
    """Answers from its script, each call taking ``delay_s`` of loop time."""

    delay_s: float = 0.0

    def bind_tools(  # type: ignore[override]
        self, tools: Sequence[Any], **kwargs: Any
    ) -> SlowScriptedModel:
        return self

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        await asyncio.sleep(self.delay_s)
        return self._generate(messages, stop=stop, **kwargs)


#: Between the 10 s platform default and the 60 s the deployment allows.
SLOW_TOOL_S = 45.0
#: Two model calls and the tool: a 105 s turn, past the 60 s platform default.
MODEL_CALL_S = 30.0
RAISED = {"total_execution_timeout_s": 240, "tool_call_timeout_s": 60}


async def slow_report(inputs: dict[str, Any], *, session: Any) -> dict[str, Any]:
    await asyncio.sleep(SLOW_TOOL_S)
    return {"rows": 3}


def _registry() -> agents_system.ToolRegistry:
    registry = agents_system.ToolRegistry()
    registry.register(
        agents_system.ToolSpec(
            name="slow_report",
            required_permissions=(),
            connector=slow_report,
            tier=agents_system.Tier.T0,
        )
    )
    return registry


def _agent(**fields: Any) -> agents_system.Agent:
    return agents_system.Agent(name="reporter", tools=("slow_report",), **fields)


def _model() -> SlowScriptedModel:
    return SlowScriptedModel(
        delay_s=MODEL_CALL_S,
        responses=[
            AIMessage(
                content="",
                tool_calls=[{"name": "slow_report", "args": {}, "id": "call-1"}],
            ),
            AIMessage(content="done"),
        ],
    )


def _runtime(
    agent: agents_system.Agent | None = None,
    *,
    build_limits: dict[str, Any] | None = None,
    **runtime_kwargs: Any,
) -> agents_system.AgentRuntime:
    equipped = agents_system.build_runtime(
        agent or _agent(), _registry(), [], execution_limits=build_limits
    )
    return agents_system.AgentRuntime(
        equipped, model=_model(), runtime_id="reporter", **runtime_kwargs
    )


async def _timed_turn(
    runtime: agents_system.AgentRuntime,
) -> tuple[list[Any], float]:
    loop = asyncio.get_running_loop()
    start = loop.time()
    messages = await runtime.run_turn(
        [HumanMessage(content="the monthly report")], session_id="s-278"
    )
    return messages, loop.time() - start


def _tool_message(messages: list[Any]) -> ToolMessage:
    (tool_message,) = [m for m in messages if isinstance(m, ToolMessage)]
    return tool_message


# ---------------------------------------------------------------------------
# A deployment-raised runtime runs the slow turn to completion
# ---------------------------------------------------------------------------


def test_the_platform_defaults_cut_the_slow_turn() -> None:
    """The control: without a deployment ceiling, the tool is cut at 10 s
    and the turn at 60 s, so the scenario below really is past both."""
    messages, elapsed = _run(_timed_turn(_runtime()))

    assert "timed out after 10" in str(_tool_message(messages).content)
    assert messages[-1].content == DEFAULT_TIMEOUT_REPLY
    assert elapsed == pytest.approx(60.0)


def test_a_deployment_raised_runtime_finishes_a_turn_past_60_seconds() -> None:
    messages, elapsed = _run(_timed_turn(_runtime(build_limits=RAISED)))

    assert "rows" in str(_tool_message(messages).content)
    assert messages[-1].content == "done"
    assert elapsed == pytest.approx(2 * MODEL_CALL_S + SLOW_TOOL_S)
    assert elapsed > 60


def test_the_runtime_argument_raises_the_limits_too() -> None:
    messages, _ = _run(_timed_turn(_runtime(execution_limits=RAISED)))

    assert "rows" in str(_tool_message(messages).content)
    assert messages[-1].content == "done"


def test_given_to_both_the_tighter_ceiling_wins() -> None:
    runtime = _runtime(
        build_limits=RAISED,
        execution_limits={"total_execution_timeout_s": 90, "tool_call_timeout_s": 50},
    )

    assert runtime.execution_limits["total_execution_timeout_s"] == 90
    assert runtime.execution_limits["tool_call_timeout_s"] == 50


def test_a_runtime_that_passes_nothing_keeps_the_platform_defaults() -> None:
    limits = _runtime().execution_limits

    assert limits["total_execution_timeout_s"] == 60
    assert limits["tool_call_timeout_s"] == 10
    assert limits["max_tool_calls"] == 20


# ---------------------------------------------------------------------------
# Only the deployment raises; the agent may tighten
# ---------------------------------------------------------------------------


def test_an_agent_definition_alone_cannot_raise_a_limit() -> None:
    raising = _agent(execution_limits={"total_execution_timeout_s": 240})

    with pytest.raises(DefinitionError, match="total_execution_timeout_s"):
        agents_system.build_runtime(raising, _registry(), [])


def test_an_agent_cannot_exceed_the_deployment_ceiling() -> None:
    raising = _agent(execution_limits={"tool_call_timeout_s": 90})

    with pytest.raises(DefinitionError, match="tool_call_timeout_s"):
        agents_system.build_runtime(raising, _registry(), [], execution_limits=RAISED)


def test_an_agent_tightens_a_deployment_raised_limit() -> None:
    """70 s and 20 s are above the platform defaults but under the
    deployment's 240 s and 60 s: a tightening of the raised ceiling, so it
    loads, and it is what runs (30 + 20 + 30 s of work is cut at 70 s)."""
    tighter = _agent(
        execution_limits={"total_execution_timeout_s": 70, "tool_call_timeout_s": 20}
    )
    runtime = _runtime(tighter, build_limits=RAISED)

    assert runtime.execution_limits["total_execution_timeout_s"] == 70
    assert runtime.execution_limits["tool_call_timeout_s"] == 20
    messages, elapsed = _run(_timed_turn(runtime))
    assert "timed out after 20" in str(_tool_message(messages).content)
    assert messages[-1].content == DEFAULT_TIMEOUT_REPLY
    assert elapsed == pytest.approx(70.0)


def test_a_declared_limit_is_capped_by_the_deployment() -> None:
    """A platform role may declare above the default (developer-agent's
    120 s turn); the deployment's value is still the ceiling."""
    declared = {"total_execution_timeout_s": 120}

    assert (
        _effective_limits(declared, {"total_execution_timeout_s": 90})[
            "total_execution_timeout_s"
        ]
        == 90
    )
    assert (
        _effective_limits(declared, {"total_execution_timeout_s": 240})[
            "total_execution_timeout_s"
        ]
        == 120
    )
    assert _effective_limits(None, {"max_tool_calls": 40})["max_tool_calls"] == 40


# ---------------------------------------------------------------------------
# Bounded by the outbox lease
# ---------------------------------------------------------------------------


def test_the_margin_and_the_derived_maximum_turn() -> None:
    assert OUTBOX_LEASE_S == 600
    assert LEASE_MARGIN_S == 60
    assert max_turn_timeout_s() == 540


@pytest.mark.parametrize(
    ("limits", "fragment"),
    [
        ({"total_execution_timeout_s": 540}, "540"),
        ({"total_execution_timeout_s": 900}, "lease"),
        ({"tool_call_timeout_s": 90}, "tool_call_timeout_s"),
        ({"total_execution_timeout_s": 120, "tool_call_timeout_s": 121}, "121"),
        ({"max_context_tokens": 1000}, "max_context_tokens"),
        ({"max_tool_calls": 0}, "max_tool_calls"),
        ({"max_tool_calls": 2.5}, "max_tool_calls"),
        ({"tool_call_timeout_s": True}, "tool_call_timeout_s"),
        ({"total_execution_timeout_s": float("nan")}, "total_execution_timeout_s"),
    ],
)
def test_a_library_runtime_refuses_a_configuration_outside_the_bounds(
    limits: dict[str, Any], fragment: str
) -> None:
    equipped = agents_system.build_runtime(_agent(), _registry(), [])

    with pytest.raises(ValueError, match="'reporter'") as excinfo:
        agents_system.AgentRuntime(
            equipped, model=_model(), runtime_id="reporter", execution_limits=limits
        )
    assert fragment in str(excinfo.value)


def test_build_runtime_refuses_a_turn_that_does_not_fit_the_lease() -> None:
    with pytest.raises(ValueError, match="lease"):
        agents_system.build_runtime(
            _agent(),
            _registry(),
            [],
            execution_limits={"total_execution_timeout_s": 600},
        )


def test_the_largest_turn_that_fits_the_lease_is_accepted() -> None:
    runtime = _runtime(
        execution_limits={"total_execution_timeout_s": 539, "tool_call_timeout_s": 120}
    )

    assert runtime.execution_limits["total_execution_timeout_s"] == 539


def test_the_runtime_logs_the_limits_it_booted_with() -> None:
    with structlog.testing.capture_logs() as logs:
        _runtime(build_limits=RAISED)

    (event,) = [e for e in logs if e["event"] == "runtime.execution_limits"]
    assert event["runtime_id"] == "reporter"
    assert event["total_execution_timeout_s"] == 240
    assert event["tool_call_timeout_s"] == 60
    assert event["max_tool_calls"] == 20
    assert event["deployment_set"] == [
        "tool_call_timeout_s",
        "total_execution_timeout_s",
    ]


# ---------------------------------------------------------------------------
# The timeout reply
# ---------------------------------------------------------------------------


def test_a_timed_out_turn_sends_the_configured_reply() -> None:
    reply = "Esto está tardando más de lo esperado. Probá de nuevo."
    messages, _ = _run(_timed_turn(_runtime(timeout_reply=reply)))

    assert messages[-1].content == reply


def test_the_reply_is_sent_verbatim_never_templated() -> None:
    reply = "Sorry {session_id} {0} %s -- try again."
    messages, _ = _run(_timed_turn(_runtime(timeout_reply=reply)))

    assert messages[-1].content == reply


@pytest.mark.parametrize("reply", ["", "   ", 42, "x" * 1001])
def test_a_blank_or_oversized_reply_is_refused(reply: Any) -> None:
    with pytest.raises(ValueError, match="'reporter'"):
        _runtime(timeout_reply=reply)


# ---------------------------------------------------------------------------
# The served app: create_app(...) and the environment
# ---------------------------------------------------------------------------


def _served_settings() -> Any:
    return _make_settings(
        adapter_runtimes=["reports"], whatsapp_checkpointer_enabled=False
    )


def _served_app(**overrides: Any) -> Any:
    return create_test_app(
        agents={"reports": "sales-agent"},
        grants={"reports": ["read:catalog"]},
        **overrides,
    )


@pytest.mark.parametrize(
    "limits",
    [
        {"total_execution_timeout_s": 540},
        {"total_execution_timeout_s": 120, "tool_call_timeout_s": 180},
    ],
)
@pytest.mark.asyncio
async def test_boot_refuses_a_configuration_outside_the_bounds_naming_the_runtime(
    limits: dict[str, Any],
) -> None:
    app = _served_app(execution_limits={"reports": limits})

    with (
        patch("agents_system.main.get_settings", return_value=_served_settings()),
        pytest.raises(ValueError, match="'reports'"),
    ):
        async with lifespan(app):
            pass


@pytest.mark.asyncio
async def test_boot_refuses_limits_or_replies_for_a_runtime_it_does_not_build() -> None:
    for overrides in (
        {"execution_limits": {"typo": RAISED}},
        {"timeout_reply": {"typo": "Un momento."}},
    ):
        app = _served_app(**overrides)
        with (
            patch("agents_system.main.get_settings", return_value=_served_settings()),
            pytest.raises(ValueError, match="typo"),
        ):
            async with lifespan(app):
                pass


@pytest.mark.asyncio
async def test_boot_hands_each_runtime_its_limits_and_reply() -> None:
    definition = MagicMock()
    definition.permissions = ("read:catalog",)
    definition.execution_limits = None
    definition.model = None
    _, _, patches = _runtime_lease_invariant_patches(definition)
    app = _served_app(
        execution_limits={"reports": RAISED},
        timeout_reply={"reports": "Un momento, por favor."},
    )

    with (
        patch("agents_system.main.get_settings", return_value=_served_settings()),
        _stack(patches),
        patch("agents_system.harness.loader.resolve", return_value=definition) as res,
        patch("agents_system.harness.factory.build_runtime") as build,
        patch("agents_system.agent.graph.AgentRuntime") as runtime_cls,
    ):
        async with lifespan(app):
            pass

    (runtime_call,) = runtime_cls.call_args_list
    assert runtime_call.kwargs["timeout_reply"] == "Un momento, por favor."
    (build_call,) = build.call_args_list
    assert build_call.kwargs["execution_limits"] == RAISED
    (resolve_call,) = res.call_args_list
    assert resolve_call.kwargs["execution_limits"] == RAISED


def test_the_environment_variables_are_validated_at_load() -> None:
    settings = _make_settings(
        execution_limits={"sales": RAISED},
        turn_timeout_reply={"sales": "Un momento."},
    )
    assert settings.execution_limits == {"sales": RAISED}
    assert settings.turn_timeout_reply == {"sales": "Un momento."}

    with pytest.raises(ValueError, match="'sales'"):
        _make_settings(execution_limits={"sales": {"total_execution_timeout_s": 600}})
    with pytest.raises(ValueError, match="'sales'"):
        _make_settings(turn_timeout_reply={"sales": "  "})
