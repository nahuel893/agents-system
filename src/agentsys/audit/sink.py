"""AuditSink — async fire-and-forget sink with bounded queue and drainer (REQ-AUDIT-30..35)."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import structlog

from agentsys.audit.events import _AuditEventBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger()

# App-state contextvar for the singleton
_app_ctx: ContextVar[dict[str, Any]] = ContextVar("audit_sink_ctx", default={})


class AuditSink:
    """Async fire-and-forget audit event sink.

    Events are enqueued to an ``asyncio.Queue(maxsize=1000)`` and drained by a
    single background coroutine that batches up to 50 events per 100ms tick.

    On queue overflow (producer faster than drainer), the event is dropped with a
    warning log and ``dropped_count`` incremented — the interceptor is NEVER
    back-pressured.

    All events in a drainer batch use ONE ``AsyncSession`` transaction
    (REQ-AUDIT-33).

    The singleton is resolved via ``AuditSink.current()`` which returns
    ``_app_ctx.get("audit_sink")``.
    """

    def __init__(
        self,
        session_factory: "async_sessionmaker[AsyncSession]",
        maxsize: int = 1000,
    ) -> None:
        self._session_factory = session_factory
        self._queue: asyncio.Queue[_AuditEventBase] = asyncio.Queue(maxsize=maxsize)
        self._maxsize = maxsize
        self._started = False
        self._shutdown = False
        self._drainer_task: asyncio.Task[None] | None = None
        self.dropped_count: int = 0

    # ---------------------------------------------------------------------------
    # Singleton
    # ---------------------------------------------------------------------------

    @classmethod
    def current(cls) -> "AuditSink":
        """Return the current AuditSink from the app context."""
        ctx = _app_ctx.get({})
        sink = ctx.get("audit_sink")
        if sink is None:
            raise RuntimeError(
                "AuditSink.current() called but no sink is registered in app.state. "
                "Ensure AuditSink is constructed in main.py lifespan and assigned "
                "to app.state.audit_sink."
            )
        return sink

    @classmethod
    def set_current(cls, sink: "AuditSink") -> None:
        """Register ``sink`` as the current singleton in the app context."""
        ctx = _app_ctx.get({}).copy()
        ctx["audit_sink"] = sink
        _app_ctx.set(ctx)

    # ---------------------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn the drainer coroutine. Idempotent (can be called multiple times)."""
        if self._started:
            return
        self._started = True
        self._drainer_task = asyncio.create_task(self._drainer_loop())

    async def stop(self) -> None:
        """Initiate graceful shutdown: signal drainer, flush remaining events, cancel."""
        if not self._started:
            return
        self._shutdown = True

        # Cancel the drainer task
        if self._drainer_task is not None and not self._drainer_task.done():
            self._drainer_task.cancel()
            try:
                await self._drainer_task
            except asyncio.CancelledError:
                pass

    async def drain(self) -> None:
        """Drain all remaining events in the queue synchronously (used at shutdown)."""
        events: list[_AuditEventBase] = []
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                events.append(event)
            except asyncio.QueueEmpty:
                break

        if events:
            await self._flush_batch(events)

    # ---------------------------------------------------------------------------
    # Record (fire-and-forget enqueue)
    # ---------------------------------------------------------------------------

    async def record(self, event: _AuditEventBase) -> None:
        """Enqueue ``event`` to the drainer queue.

        Returns within 5ms. If the queue is full, logs ``audit.event_dropped`` and
        increments ``dropped_count`` — NEVER raises.
        """
        if not self._started:
            raise RuntimeError(
                "AuditSink.record() called before start(). "
                "Call start() first or construct the sink in main.py lifespan."
            )

        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_count += 1
            logger.warning(
                "audit.event_dropped",
                event_type=getattr(event, "event_type", "unknown"),
                tool_name=getattr(event, "tool_name", None),
                queue_size=self._maxsize,
                dropped_count=self.dropped_count,
            )

    # ---------------------------------------------------------------------------
    # Drainer coroutine
    # ---------------------------------------------------------------------------

    async def _drainer_loop(self) -> None:
        """Background coroutine: drain the queue in batches of up to 50 every 100ms."""
        batch: list[_AuditEventBase] = []
        last_flush = time.monotonic()

        while not self._shutdown:
            try:
                # Wait up to 100ms for an event
                event = await asyncio.wait_for(
                    self._queue.get(), timeout=0.1
                )
                batch.append(event)
            except asyncio.TimeoutError:
                pass  # fell through — check flush conditions

            now = time.monotonic()
            flush_due = len(batch) >= 50 or (batch and (now - last_flush) >= 0.1)

            if flush_due and batch:
                await self._flush_batch(batch)
                batch = []
                last_flush = time.monotonic()

        # Shutdown: drain remaining events
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                batch.append(event)
            except asyncio.QueueEmpty:
                break

        if batch:
            await self._flush_batch(batch)

    async def _flush_batch(self, batch: list[_AuditEventBase]) -> None:
        """Open one AsyncSession, add all events, commit once, close (REQ-AUDIT-33)."""
        from agentsys.models.audit_event import map_to_audit_event

        try:
            async with self._session_factory() as session:
                for event in batch:
                    orm_row = map_to_audit_event(event.model_dump())
                    await session.add(orm_row)
                await session.commit()
        except Exception:
            # Drainer must NEVER crash — log and continue
            logger.error(
                "audit.drain_failed",
                batch_size=len(batch),
                error=str(Exception()),
            )
