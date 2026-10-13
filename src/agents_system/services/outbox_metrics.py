"""The permanent-failure counter of the outbox worker (#290).

Built on an explicit registry like every metric in
``observability/metrics.py``; ``DEFAULT_OUTBOX_FAILURES`` is bound to the
registry ``GET /metrics`` serves. Labels are bounded: ``channel`` is a
registered channel name, ``reason`` one of ``OUTBOX_FAILURE_REASONS`` and
``exception`` the class name of the exception (never its message, which may
carry user or tool data).
"""

from __future__ import annotations

import re

from prometheus_client import CollectorRegistry, Counter

from agents_system.observability.metrics import DEFAULT_METRICS

#: ``permanent``: the failure will repeat, so it was not retried.
#: ``retries_exhausted``: a transient failure that outlasted the retries.
OUTBOX_FAILURE_REASONS = ("permanent", "retries_exhausted")

_MAX_CLASS_NAME = 64
_NOT_A_NAME = re.compile(r"[^A-Za-z0-9_]")


def exception_label(exc: BaseException) -> str:
    """The class name of *exc*, safe as a label value."""
    return _NOT_A_NAME.sub("_", type(exc).__name__)[:_MAX_CLASS_NAME] or "Exception"


def build_outbox_failure_counter(registry: CollectorRegistry) -> Counter:
    return Counter(
        "outbox_permanent_failures_total",
        "Outbox work items that failed for good, by channel, exception class "
        "and whether the failure was permanent or exhausted its retries.",
        ["channel", "exception", "reason"],
        registry=registry,
    )


DEFAULT_OUTBOX_FAILURES = build_outbox_failure_counter(DEFAULT_METRICS.registry)
