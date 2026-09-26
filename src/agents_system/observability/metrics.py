"""Prometheus metrics -- issue #78 Phase 0 Slice 2 (process, turn, tool).

Design (ADR-005 section 6, "a minimal counter set (tool calls, denials,
limit trips) before any broader OpenTelemetry rollout"):

- Every metric is built against an explicit `CollectorRegistry` via
  `build_metrics()`, never bound to `prometheus_client`'s own ambient
  `REGISTRY` singleton. `DEFAULT_METRICS`/`DEFAULT_REGISTRY` below are the
  process-wide set `GET /metrics` serves; a test builds its own throw-away
  set with `build_metrics(CollectorRegistry())` so tests never collide with
  each other or with the process-wide registry (registering the same metric
  name twice on one registry raises `ValueError`).
- `build_metrics` also registers `prometheus_client`'s default
  `ProcessCollector` on the same registry, which on Linux exports resident
  memory (`process_resident_memory_bytes`) and CPU seconds
  (`process_cpu_seconds_total`) for the running process -- see
  `tests/test_observability_metrics.py` for a runtime-confirming test rather
  than trusting the library's docs alone.
- Labels are bounded and reviewed here, never a caller-supplied ceiling:
  `runtime_id` (the operator-configured `"{deployment}__{role}"` id, or an
  `AgentRuntime`'s own derived provider-model id when no explicit one was
  given -- see `agent/graph.py::AgentRuntime`), `outcome`/`limit`/`tool`/
  `direction` (fixed, code-defined enums). NEVER a user id, phone number,
  correlation/request id, or message text -- those are unbounded and would
  make Prometheus's cardinality blow up, besides leaking PII into a metrics
  endpoint. The recording functions below take only these bounded values,
  by construction -- there is no parameter through which an unbounded value
  could reach a label.
"""

from __future__ import annotations

import dataclasses

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.process_collector import ProcessCollector

#: Turn-level outcomes `AgentRuntime.run_turn_with_usage` reports (graph.py).
#: "ok" also covers a turn that legitimately ended at the `_limit_reached`
#: node -- that is a normal completion; `limit_trips_total` is the counter
#: for the limit itself firing.
TURN_OUTCOMES = ("ok", "timeout", "error")

#: Tool-call outcomes `_execute_tools` reports (graph.py). "denied" is a
#: `PolicyViolation` for a tool never in the equipped surface at all
#: (reason="not_in_surface" -- Layer-1 territory, e.g. a hallucinated tool
#: name); "blocked" is a Layer-2 revalidation failure on a tool that WAS
#: equipped (reason in {"revalidation_required", "permission_revoked"}).
TOOL_CALL_OUTCOMES = ("ok", "denied", "blocked", "timeout", "error")

#: Seconds. A turn can legitimately run for tens of seconds (several model
#: + tool round trips under total_execution_timeout_s); the top bucket is
#: effectively "at or beyond a very slow turn".
_TURN_DURATION_BUCKETS_S = (0.1, 0.5, 1, 2, 5, 10, 20, 30, 60, 120, float("inf"))

#: Seconds. One tool call is bounded by tool_call_timeout_s (default 10s,
#: `harness/loader.PLATFORM_DEFAULT_LIMITS`); buckets stay well under and
#: around that default.
_TOOL_CALL_DURATION_BUCKETS_S = (0.01, 0.05, 0.1, 0.5, 1, 2, 5, 10, 30, float("inf"))


@dataclasses.dataclass(frozen=True)
class Metrics:
    """One bound set of this slice's Prometheus collectors.

    Never construct `Counter`/`Histogram` at module scope directly -- always
    go through `build_metrics(registry)`, which is what makes a fresh,
    isolated set possible per test.
    """

    registry: CollectorRegistry
    turns_total: Counter
    turn_duration_seconds: Histogram
    tool_calls_total: Counter
    tool_call_duration_seconds: Histogram
    limit_trips_total: Counter
    tokens_total: Counter
    cost_usd_total: Counter


def build_metrics(registry: CollectorRegistry) -> Metrics:
    """Register this slice's counters/histograms, plus the default process
    collector (resident memory + CPU seconds on Linux), on *registry*.

    Called once at import time for the process-wide `DEFAULT_METRICS`
    (bound to `DEFAULT_REGISTRY`); call it again with a fresh
    `CollectorRegistry()` in a test for an isolated set that exercises the
    exact same recording functions without touching process-wide state.
    """
    ProcessCollector(registry=registry)

    return Metrics(
        registry=registry,
        turns_total=Counter(
            "agent_turns",
            "Turns completed, by runtime id and outcome.",
            ["runtime_id", "outcome"],
            registry=registry,
        ),
        turn_duration_seconds=Histogram(
            "agent_turn_duration_seconds",
            "Wall-clock duration of a turn, by runtime id and outcome.",
            ["runtime_id", "outcome"],
            buckets=_TURN_DURATION_BUCKETS_S,
            registry=registry,
        ),
        tool_calls_total=Counter(
            "agent_tool_calls",
            "Tool calls attempted, by tool name and outcome.",
            ["tool", "outcome"],
            registry=registry,
        ),
        tool_call_duration_seconds=Histogram(
            "agent_tool_call_duration_seconds",
            "Wall-clock duration of one tool call, by tool name and outcome.",
            ["tool", "outcome"],
            buckets=_TOOL_CALL_DURATION_BUCKETS_S,
            registry=registry,
        ),
        limit_trips_total=Counter(
            "agent_limit_trips",
            "Execution limits that fired, by limit name.",
            ["limit"],
            registry=registry,
        ),
        tokens_total=Counter(
            "agent_tokens",
            "Tokens accounted, by runtime id and direction (input/output).",
            ["runtime_id", "direction"],
            registry=registry,
        ),
        cost_usd_total=Counter(
            "agent_cost_usd",
            "Estimated USD cost of tokens billed, by runtime id.",
            ["runtime_id"],
            registry=registry,
        ),
    )


#: The process-wide registry `GET /metrics` serves (`main.py`). Built once
#: at import time -- never touched by tests (see module docstring).
DEFAULT_REGISTRY = CollectorRegistry()
DEFAULT_METRICS = build_metrics(DEFAULT_REGISTRY)


def record_turn(
    metrics: Metrics,
    *,
    runtime_id: str,
    outcome: str,
    duration_s: float,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
    """Record one completed turn (`AgentRuntime.run_turn_with_usage`).

    Honesty rule, same as `agent.graph.TurnUsage`: `input_tokens`/
    `output_tokens`/`cost_usd` of `None` (genuinely unknown -- e.g. a timed
    out turn) are simply not counted, never recorded as a guessed `0`.
    """
    metrics.turns_total.labels(runtime_id=runtime_id, outcome=outcome).inc()
    metrics.turn_duration_seconds.labels(
        runtime_id=runtime_id, outcome=outcome
    ).observe(duration_s)
    if input_tokens is not None:
        metrics.tokens_total.labels(runtime_id=runtime_id, direction="input").inc(
            input_tokens
        )
    if output_tokens is not None:
        metrics.tokens_total.labels(runtime_id=runtime_id, direction="output").inc(
            output_tokens
        )
    if cost_usd is not None:
        metrics.cost_usd_total.labels(runtime_id=runtime_id).inc(cost_usd)


def record_tool_call(
    metrics: Metrics, *, tool: str, outcome: str, duration_s: float
) -> None:
    """Record one attempted tool call (`_execute_tools` in `agent/graph.py`)."""
    metrics.tool_calls_total.labels(tool=tool, outcome=outcome).inc()
    metrics.tool_call_duration_seconds.labels(tool=tool, outcome=outcome).observe(
        duration_s
    )


def record_limit_trip(metrics: Metrics, *, limit: str) -> None:
    """Record one execution limit firing (e.g. `_limit_reached` -- `max_tool_calls`)."""
    metrics.limit_trips_total.labels(limit=limit).inc()
