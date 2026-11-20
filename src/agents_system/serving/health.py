"""Provider probe and outbox backlog for the health endpoint."""

from __future__ import annotations

import asyncio
import time
from collections.abc import (
    Awaitable,
    Callable,
)
from typing import Any

import httpx
import structlog

from agents_system.config import Settings
from agents_system.models.base import get_session_factory
from agents_system.services.outbox import (
    OutboxBacklogCounts,
    count_outbox_backlog,
)

_PROVIDER_PROBE_TIMEOUT_S = 1.0


_PROVIDER_PROBE_CACHE_TTL_S = 5.0


def _provider_probe_target(settings: Settings) -> str:
    """Return the credential-free base URL used for a provider liveness probe.

    A HEAD against the provider base URL cannot generate tokens. Any HTTP
    response, including an authentication or route error, proves the provider
    is reachable; transport and timeout failures do not. The configured
    OpenAI-compatible and Ollama base URLs are used verbatim (minus a trailing
    slash), while hosted providers have fixed public API origins.
    """
    if settings.adapter_provider == "ollama":
        return (settings.ollama_base_url or "http://localhost:11434").rstrip("/")
    if settings.adapter_provider == "openai_compatible":
        return settings.openai_compatible_base_url.rstrip("/")
    if settings.adapter_provider == "groq":
        return "https://api.groq.com"
    return "https://api.anthropic.com"


class ProviderHealthProbe:
    """Bounded, cached reachability probe that never sends provider credentials."""

    def __init__(
        self,
        *,
        request: Callable[[str], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._request = request or self._head
        self._monotonic = monotonic
        self._cached: dict[str, tuple[float, bool]] = {}
        self._lock = asyncio.Lock()

    async def _head(self, target: str) -> None:
        async with httpx.AsyncClient(
            timeout=_PROVIDER_PROBE_TIMEOUT_S,
            follow_redirects=False,
        ) as client:
            await client.head(target)

    async def probe(self, settings: Settings) -> bool:
        """Return reachability, using a short per-provider-target cache."""
        target = _provider_probe_target(settings)
        cache_key = f"{settings.adapter_provider}:{target}"
        now = self._monotonic()
        cached = self._cached.get(cache_key)
        if cached is not None and now - cached[0] < _PROVIDER_PROBE_CACHE_TTL_S:
            return cached[1]

        async with self._lock:
            now = self._monotonic()
            cached = self._cached.get(cache_key)
            if cached is not None and now - cached[0] < _PROVIDER_PROBE_CACHE_TTL_S:
                return cached[1]

            try:
                async with asyncio.timeout(_PROVIDER_PROBE_TIMEOUT_S):
                    await self._request(target)
            except Exception:
                # Do not log the exception: provider error strings can include a
                # credential-bearing URL or response details.
                structlog.get_logger().warning(
                    "health.provider_unreachable", provider=settings.adapter_provider
                )
                reachable = False
            else:
                reachable = True
            self._cached[cache_key] = (now, reachable)
            return reachable


async def _outbox_backlog(engine: Any) -> OutboxBacklogCounts | None:
    """Best-effort outbox backlog counts for GET /health (#141).

    ``None`` on any failure -- opening a session or querying can fail for
    the same reasons the postgres probe can (already reported separately as
    ``postgres_status``), and that must not crash the health check itself.
    ``None`` is deliberately not coerced to ``0``: an unreachable/unknown
    backlog is a different fact than a confirmed-empty one, and collapsing
    them would let a DB outage report a falsely healthy backlog.

    Bounded to the same 3s budget as the postgres/redis probes above (#141
    review follow-up, BLOCKER) -- ``outbox_work`` is queried through the
    same engine those probes use, and without a timeout here a locked or
    slow table would hang this whole endpoint (and any liveness probe
    reading it) even while postgres/redis themselves answer fine. A timeout
    is just another failure from this function's point of view: it is
    caught below and reported as ``None``, the same as any other error.
    """
    try:
        async with asyncio.timeout(3.0):
            session_factory = get_session_factory(engine)
            async with session_factory() as session:
                return await count_outbox_backlog(session)
    except Exception:
        structlog.get_logger().warning("health.outbox_backlog_error")
        return None
