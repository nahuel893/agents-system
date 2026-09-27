"""Observability package — logging configuration, request middleware, metrics."""

from agents_system.observability.logging import (
    request_id_ctx,
    setup_logging,
    thread_id_ctx,
)
from agents_system.observability.metrics import (
    DEFAULT_METRICS,
    DEFAULT_REGISTRY,
    Metrics,
    build_metrics,
    record_limit_trip,
    record_provider_request,
    record_provider_retry,
    record_tool_call,
    record_turn,
)
from agents_system.observability.middleware import RequestIdMiddleware
from agents_system.observability.provider_http import (
    MetricsHTTPTransport,
    classify_provider_status,
)

__all__ = [
    "DEFAULT_METRICS",
    "DEFAULT_REGISTRY",
    "Metrics",
    "MetricsHTTPTransport",
    "RequestIdMiddleware",
    "build_metrics",
    "classify_provider_status",
    "record_limit_trip",
    "record_provider_request",
    "record_provider_retry",
    "record_tool_call",
    "record_turn",
    "request_id_ctx",
    "setup_logging",
    "thread_id_ctx",
]
