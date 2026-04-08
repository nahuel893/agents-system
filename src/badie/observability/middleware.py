"""Request ID middleware — generates and binds a per-request correlation ID."""

from __future__ import annotations

import time
from uuid import uuid4

import structlog.contextvars
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from badie.observability.logging import logger


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Generates a short request_id, binds it to structlog contextvars, and logs lifecycle events."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        request_id = uuid4().hex[:8]
        structlog.contextvars.bind_contextvars(request_id=request_id)
        logger.info("request.started", method=request.method, path=request.url.path)
        start = time.monotonic()
        try:
            response = await call_next(request)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "request.completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                elapsed_ms=elapsed_ms,
            )
            return response
        finally:
            structlog.contextvars.clear_contextvars()
