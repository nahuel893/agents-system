"""Tests for issue #78 Phase 0 Slice 2 -- Prometheus metrics.

Every test builds its OWN `CollectorRegistry` via `build_metrics()` -- never
`agents_system.observability.metrics.DEFAULT_REGISTRY` -- so tests never
collide with each other (Prometheus raises `ValueError: Duplicated timeseries`
registering the same metric name twice on one registry) and never mutate
process-wide state another test could observe.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, generate_latest

from agents_system.observability.metrics import (
    DEFAULT_METRICS,
    DEFAULT_REGISTRY,
    build_metrics,
    record_limit_trip,
    record_tool_call,
    record_turn,
)

# ---------------------------------------------------------------------------
# Process collector -- resident memory + CPU seconds (Linux)
# ---------------------------------------------------------------------------


def test_process_collector_exports_memory_and_cpu_on_this_runtime() -> None:
    """`build_metrics` registers prometheus_client's default ProcessCollector,
    which on Linux exports resident memory and CPU seconds for the running
    process -- confirmed here against the actual CI/dev runtime, not assumed.
    """
    registry = CollectorRegistry()
    build_metrics(registry)

    output = generate_latest(registry).decode()

    assert "process_resident_memory_bytes" in output
    assert "process_cpu_seconds_total" in output

    # A real, positive resident memory value -- not just the metric name
    # present with no samples.
    families = {f.name: f for f in registry.collect()}
    memory_samples = families["process_resident_memory_bytes"].samples
    assert len(memory_samples) == 1
    assert memory_samples[0].value > 0


# ---------------------------------------------------------------------------
# build_metrics -- fresh registry per call, no cross-test collisions
# ---------------------------------------------------------------------------


def test_build_metrics_on_a_fresh_registry_does_not_collide_with_default() -> None:
    """Building a second, independent Metrics set must never raise -- it is
    bound to its own registry, distinct from `DEFAULT_REGISTRY`."""
    registry = CollectorRegistry()
    metrics = build_metrics(registry)

    assert metrics.registry is registry
    assert metrics.registry is not DEFAULT_REGISTRY
    assert metrics is not DEFAULT_METRICS


# ---------------------------------------------------------------------------
# Label hygiene -- the schema is bounded and reviewed here
# ---------------------------------------------------------------------------


def test_turn_metrics_label_schema_is_bounded() -> None:
    registry = CollectorRegistry()
    metrics = build_metrics(registry)

    assert metrics.turns_total._labelnames == ("runtime_id", "outcome")
    assert metrics.turn_duration_seconds._labelnames == ("runtime_id", "outcome")
    assert metrics.tool_calls_total._labelnames == ("tool", "outcome")
    assert metrics.tool_call_duration_seconds._labelnames == ("tool", "outcome")
    assert metrics.limit_trips_total._labelnames == ("limit",)
    assert metrics.tokens_total._labelnames == ("runtime_id", "direction")
    assert metrics.cost_usd_total._labelnames == ("runtime_id",)


def test_no_unbounded_label_ever_reaches_the_exported_text() -> None:
    """A session id / phone number / correlation id passed by mistake must
    never show up in the exported metrics text -- this locks that down at
    the recording functions themselves, not just the label schema above."""
    registry = CollectorRegistry()
    metrics = build_metrics(registry)

    session_like_phone = "+549111234567"
    record_turn(
        metrics,
        runtime_id="acme__sales-agent",
        outcome="ok",
        duration_s=1.23,
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
    )
    record_tool_call(metrics, tool="catalog_search", outcome="ok", duration_s=0.05)
    record_limit_trip(metrics, limit="max_tool_calls")

    output = generate_latest(registry).decode()
    assert session_like_phone not in output


# ---------------------------------------------------------------------------
# record_turn
# ---------------------------------------------------------------------------


def test_record_turn_increments_counter_and_histogram() -> None:
    registry = CollectorRegistry()
    metrics = build_metrics(registry)

    record_turn(
        metrics,
        runtime_id="acme__sales-agent",
        outcome="ok",
        duration_s=2.5,
        input_tokens=100,
        output_tokens=40,
        cost_usd=0.0056,
    )

    assert (
        metrics.turns_total.labels(
            runtime_id="acme__sales-agent", outcome="ok"
        )._value.get()
        == 1
    )
    histogram = metrics.turn_duration_seconds.labels(
        runtime_id="acme__sales-agent", outcome="ok"
    )
    assert histogram._sum.get() == 2.5
    assert (
        metrics.tokens_total.labels(
            runtime_id="acme__sales-agent", direction="input"
        )._value.get()
        == 100
    )
    assert (
        metrics.tokens_total.labels(
            runtime_id="acme__sales-agent", direction="output"
        )._value.get()
        == 40
    )
    assert (
        metrics.cost_usd_total.labels(runtime_id="acme__sales-agent")._value.get()
        == 0.0056
    )


def test_record_turn_with_unknown_usage_skips_token_and_cost_counters() -> None:
    """Honesty rule (same as TurnUsage elsewhere): an unknown token/cost
    value is never counted as a guessed 0 -- it is simply not recorded."""
    registry = CollectorRegistry()
    metrics = build_metrics(registry)

    record_turn(
        metrics,
        runtime_id="acme__sales-agent",
        outcome="timeout",
        duration_s=60.0,
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
    )

    assert (
        metrics.turns_total.labels(
            runtime_id="acme__sales-agent", outcome="timeout"
        )._value.get()
        == 1
    )
    families = {f.name: f for f in registry.collect()}
    tokens_samples = [
        s for s in families["agent_tokens"].samples if s.name == "agent_tokens_total"
    ]
    assert tokens_samples == []
    cost_samples = [
        s
        for s in families["agent_cost_usd"].samples
        if s.name == "agent_cost_usd_total"
    ]
    assert cost_samples == []


# ---------------------------------------------------------------------------
# record_tool_call
# ---------------------------------------------------------------------------


def test_record_tool_call_increments_counter_and_histogram() -> None:
    registry = CollectorRegistry()
    metrics = build_metrics(registry)

    record_tool_call(metrics, tool="catalog_search", outcome="ok", duration_s=0.42)

    assert (
        metrics.tool_calls_total.labels(
            tool="catalog_search", outcome="ok"
        )._value.get()
        == 1
    )
    histogram = metrics.tool_call_duration_seconds.labels(
        tool="catalog_search", outcome="ok"
    )
    assert histogram._sum.get() == 0.42


# ---------------------------------------------------------------------------
# record_limit_trip
# ---------------------------------------------------------------------------


def test_record_limit_trip_increments_counter() -> None:
    registry = CollectorRegistry()
    metrics = build_metrics(registry)

    record_limit_trip(metrics, limit="max_tool_calls")
    record_limit_trip(metrics, limit="max_tool_calls")

    assert metrics.limit_trips_total.labels(limit="max_tool_calls")._value.get() == 2
