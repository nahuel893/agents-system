"""Observability package — logging configuration, request middleware, metrics.

``RequestIdMiddleware`` is resolved on first access: it is an HTTP middleware
(the ``server`` extra), while logging and metrics are core and imported by the
in-process runtime (issue #188).
"""

from typing import Any

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


def __getattr__(name: str) -> Any:
    if name == "RequestIdMiddleware":
        from agents_system.observability.middleware import RequestIdMiddleware

        return RequestIdMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
