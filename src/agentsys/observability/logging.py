"""Structured logging configuration using structlog."""

from __future__ import annotations

from contextvars import ContextVar

import structlog
import structlog.contextvars

request_id_ctx: ContextVar[str | None] = ContextVar("request_id_ctx", default=None)
thread_id_ctx: ContextVar[str | None] = ContextVar("thread_id_ctx", default=None)

logger = structlog.get_logger()


def setup_logging() -> None:
    """Configure structlog with JSON processors. Idempotent — safe to call multiple times."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
