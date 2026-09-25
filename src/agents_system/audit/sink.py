"""AuditSink — async fire-and-forget sink with bounded queue and drainer (REQ-AUDIT-30..35)."""

from __future__ import annotations

import asyncio
import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from agents_system.audit.events import _AuditEventBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger()

#: Atomic per-correlation sequence allocation (issue #9, ADR-001 D-042).
#:
#: ``audit_sequence`` (migration 005) holds one row per ``correlation_id``,
#: the next value to hand out. This upsert is the classic PostgreSQL atomic
#: counter pattern: PostgreSQL's own row lock on the ``correlation_id`` row
#: serializes concurrent writers from ANY process, which is what makes the
#: allocated value safe across N worker processes — the former
#: ``recorder._allocate_sequence`` was a per-process, in-memory counter and
#: could not offer that guarantee no matter how it was implemented.
_ALLOCATE_SEQUENCE_SQL = text(
    """
    INSERT INTO audit_sequence (correlation_id, next_seq)
    VALUES (:correlation_id, 1)
    ON CONFLICT (correlation_id)
    DO UPDATE SET next_seq = audit_sequence.next_seq + 1
    RETURNING next_seq
    """
)

# App-state contextvar for the singleton. No shared mutable default: every
# `.get(...)` call site below supplies its own fresh `{}`.
_app_ctx: ContextVar[dict[str, Any]] = ContextVar("audit_sink_ctx")


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
        session_factory: async_sessionmaker[AsyncSession],
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
    def current(cls) -> AuditSink:
        """Return the current AuditSink from the app context."""
        ctx = _app_ctx.get({})
        sink = ctx.get("audit_sink")
        if sink is None:
            raise RuntimeError(
                "AuditSink.current() called but no sink is registered in app.state. "
                "Ensure AuditSink is constructed in main.py lifespan and assigned "
                "to app.state.audit_sink."
            )
        assert isinstance(sink, AuditSink)
        return sink

    @classmethod
    def set_current(cls, sink: AuditSink) -> None:
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

    async def stop(self, timeout: float = 5.0) -> None:
        """Initiate graceful shutdown: signal the drainer and AWAIT its own exit.

        Setting ``_shutdown`` makes ``_drainer_loop``'s ``while not self._shutdown``
        condition false on its next check; its shutdown-drain tail (whatever is
        left in ``self._queue``, plus any batch already accumulated in-flight)
        runs right after that loop exits and flushes once. Awaiting the task
        (instead of cancelling it immediately, the previous bug) is what lets
        that tail actually run: cancelling almost always lands inside the
        drainer's ``await asyncio.wait_for(self._queue.get(), ...)``, which
        raises ``CancelledError`` out of the loop before the shutdown tail is
        ever reached, discarding every event still queued or in-flight.

        Bounded by ``timeout`` so a wedged flush (e.g. a stuck DB call) cannot
        hang shutdown forever. On timeout, ``asyncio.wait_for`` cancels the
        drainer task itself (last resort) and this counts whatever is still
        sitting in the queue as dropped.
        """
        if not self._started:
            return
        self._shutdown = True

        if self._drainer_task is not None and not self._drainer_task.done():
            try:
                await asyncio.wait_for(self._drainer_task, timeout=timeout)
            except TimeoutError:
                remaining = self._queue.qsize()
                self.dropped_count += remaining
                logger.warning(
                    "audit.shutdown_timeout",
                    timeout_s=timeout,
                    queued_events_dropped=remaining,
                )
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
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                batch.append(event)
            except TimeoutError:
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
        """Open one AsyncSession, add all events, commit once, close (REQ-AUDIT-33).

        Each event's ``sequence`` (a recorder-time placeholder — see
        ``recorder.py``) is overwritten here with the real, authoritative
        value, allocated atomically per event via ``_ALLOCATE_SEQUENCE_SQL``:
        a classic PostgreSQL upsert against ``audit_sequence`` (migration
        005), executed on THIS SAME session, so it commits or rolls back with
        the rest of the batch — a failed batch releases its "reserved"
        numbers for free. PostgreSQL's own row lock on that
        ``correlation_id``'s ``audit_sequence`` row is what serializes
        concurrent writers across ANY process (issue #9, ADR-001 D-042) — the
        actual fix for two workers emitting a contextless event in the same
        instant. Allocated one event at a time, in batch/queue (FIFO) order,
        so ordering within one ``correlation_id`` is preserved.
        """
        from agents_system.models.audit_event import map_to_audit_event

        try:
            async with self._session_factory() as session:
                for event in batch:
                    event_data = event.model_dump()
                    result = await session.execute(
                        _ALLOCATE_SEQUENCE_SQL,
                        {"correlation_id": event_data["correlation_id"]},
                    )
                    event_data["sequence"] = result.scalar_one()
                    orm_row = map_to_audit_event(event_data)
                    session.add(orm_row)  # synchronous: awaiting None raises
                await session.commit()
        except Exception as exc:
            # Drainer must NEVER crash — log and continue.
            # `exc` must be bound: `str(Exception())` constructs a fresh empty
            # exception and stringifies THAT, so the only record of a failed
            # audit write carried an empty string.
            logger.exception(
                "audit.drain_failed",
                batch_size=len(batch),
                error=str(exc),
            )
