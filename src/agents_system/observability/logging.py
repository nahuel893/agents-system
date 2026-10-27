"""Structured logging configuration using structlog."""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar

import structlog
import structlog.contextvars

request_id_ctx: ContextVar[str | None] = ContextVar("request_id_ctx", default=None)
thread_id_ctx: ContextVar[str | None] = ContextVar("thread_id_ctx", default=None)

logger = structlog.get_logger()

#: An output-file download path carries its bearer-free capability token
#: (#319): whoever reads the logs must not be able to download the file.
#: Not anchored: behind ``--root-path`` the logged path starts with the prefix.
_DOWNLOAD_TOKEN = re.compile(r"(/v1/files/)[^/?#\s]+")


def loggable_path(path: str) -> str:
    """``path`` with an output-file download token replaced by
    ``<redacted>``; any other path unchanged."""
    return _DOWNLOAD_TOKEN.sub(r"\1<redacted>", path)


class DownloadTokenFilter(logging.Filter):
    """Redacts output-file download tokens in uvicorn's access log.

    uvicorn logs each request as ``'%s - "%s %s HTTP/%s" %d'`` with the path
    and query string among ``record.args``; this rewrites that argument
    before the record is formatted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                loggable_path(arg) if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


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
    # #319 review, S1: uvicorn's access log is on by default and would write
    # every download URL, token included.
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, DownloadTokenFilter) for f in access.filters):
        access.addFilter(DownloadTokenFilter())
