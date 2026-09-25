"""AuditSink — async fire-and-forget sink with bounded queue and drainer (REQ-AUDIT-30..35)."""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import text

from agents_system.audit.events import _AuditEventBase

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = structlog.get_logger()

#: Atomic per-correlation sequence RANGE reservation (issue #9, ADR-001 D-042).
#:
#: ``audit_sequence`` (migration 005) holds one row per ``correlation_id``:
#: ``next_seq`` is the LAST sequence value handed out for it. This upsert is
#: the classic PostgreSQL atomic-counter pattern, widened to reserve
#: ``:count`` values at once: it returns the last value of the reserved range,
#: so the range is ``returned - count + 1 .. returned``. PostgreSQL's own row
#: lock on the ``correlation_id`` row serializes concurrent writers from ANY
#: process, which is what makes the reservation safe across N worker
#: processes — the former ``recorder._allocate_sequence`` was a per-process,
#: in-memory counter and could not offer that guarantee.
#:
#: ``EXCLUDED.next_seq`` is the row that failed to insert, i.e. ``:count``,
#: so the parameter is bound once.
_RESERVE_SEQUENCE_RANGE_SQL = text(
    """
    INSERT INTO audit_sequence (correlation_id, next_seq)
    VALUES (:correlation_id, :count)
    ON CONFLICT (correlation_id)
    DO UPDATE SET next_seq = audit_sequence.next_seq + EXCLUDED.next_seq
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
        # stop() has begun: the drainer finishes its current flush, then exits.
        self._shutdown = False
        # The drainer's final sweep has run (or stop() gave up on it): nothing
        # will ever read the queue again, so record() must reject, not enqueue.
        self._closed = False
        # Events taken off the queue but not yet committed or counted as
        # dropped -- what a cancelled drainer would otherwise lose silently.
        self._pending: list[_AuditEventBase] = []
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
        drainer task itself (last resort).

        Then the sink is marked closed -- ``record()`` rejects from here on --
        and every event that was neither written nor already counted goes
        into ``dropped_count`` (PR #72 review, findings 3 and 5): the batch a
        cancelled drainer was flushing, and whatever is still queued because
        no drainer swept it (timed out, or died before ``stop()``). After a
        clean drainer exit both are empty and nothing is logged. A batch
        whose commit landed in the instant before a timeout cancelled its
        flush is counted too: over-reporting loss beats hiding it.
        """
        if not self._started:
            return
        self._shutdown = True

        timed_out = False
        if self._drainer_task is not None and not self._drainer_task.done():
            try:
                await asyncio.wait_for(self._drainer_task, timeout=timeout)
            except TimeoutError:
                timed_out = True
            except asyncio.CancelledError:
                pass

        self._closed = True
        in_flight = len(self._pending)
        self._pending = []
        queued = len(self._take_queued())
        self.dropped_count += in_flight + queued
        if timed_out:
            logger.warning(
                "audit.shutdown_timeout",
                timeout_s=timeout,
                queued_events_dropped=queued,
                in_flight_events_dropped=in_flight,
                dropped_count=self.dropped_count,
            )
        elif in_flight or queued:
            logger.warning(
                "audit.shutdown_events_dropped",
                queued_events_dropped=queued,
                in_flight_events_dropped=in_flight,
                dropped_count=self.dropped_count,
            )

    async def drain(self) -> None:
        """Drain all remaining events in the queue synchronously (used at shutdown)."""
        events = self._take_queued()
        if events:
            await self._flush_batch(events)

    def _take_queued(self) -> list[_AuditEventBase]:
        """Remove and return everything currently queued, without awaiting."""
        events: list[_AuditEventBase] = []
        while True:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return events

    # ---------------------------------------------------------------------------
    # Record (fire-and-forget enqueue)
    # ---------------------------------------------------------------------------

    async def record(self, event: _AuditEventBase) -> None:
        """Enqueue ``event`` to the drainer queue.

        Returns within 5ms. If the queue is full, logs ``audit.event_dropped`` and
        increments ``dropped_count`` — NEVER raises.

        Once the sink is closed (the drainer's final sweep has run, or
        ``stop()`` finished) nothing will ever read the queue again, so the
        event is counted as dropped and logged as ``audit.event_dropped_shutdown``
        instead of being enqueued to sit in memory unwritten and uncounted
        (PR #72 review, finding 3). Before that point -- including while
        ``stop()`` waits for the drainer -- the event is still accepted,
        because that final sweep will write it.
        """
        if not self._started:
            raise RuntimeError(
                "AuditSink.record() called before start(). "
                "Call start() first or construct the sink in main.py lifespan."
            )

        if self._closed:
            self.dropped_count += 1
            logger.warning(
                "audit.event_dropped_shutdown",
                event_type=getattr(event, "event_type", "unknown"),
                tool_name=getattr(event, "tool_name", None),
                dropped_count=self.dropped_count,
            )
            return

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
        """Background coroutine: drain the queue in batches of up to 50 every 100ms.

        The batch being built or flushed lives in ``self._pending`` so that
        ``stop()`` can count it if it has to cancel this task mid-flush.
        """
        last_flush = time.monotonic()

        while not self._shutdown:
            try:
                # Wait up to 100ms for an event
                event = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                self._pending.append(event)
            except TimeoutError:
                pass  # fell through — check flush conditions

            now = time.monotonic()
            flush_due = len(self._pending) >= 50 or (
                bool(self._pending) and (now - last_flush) >= 0.1
            )

            if flush_due:
                await self._flush_pending()
                last_flush = time.monotonic()

        # Shutdown: close, then sweep what is left -- with no await in between,
        # so every event record() accepted is in this final batch, and every
        # later one is rejected and counted instead of stranded.
        self._closed = True
        self._pending.extend(self._take_queued())
        if self._pending:
            await self._flush_pending()

    async def _flush_pending(self) -> None:
        """Flush ``self._pending``; afterwards it is committed or counted."""
        await self._flush_batch(self._pending)
        self._pending = []

    @staticmethod
    async def _reserve_sequences(
        session: AsyncSession, batch: list[_AuditEventBase]
    ) -> list[int]:
        """Reserve every sequence ``batch`` needs; return them in batch order.

        One ``_RESERVE_SEQUENCE_RANGE_SQL`` upsert per DISTINCT
        ``correlation_id``, reserving that correlation's whole contiguous
        range at once, issued in SORTED ``correlation_id`` order.

        The sort is the deadlock fix (PR #72 review, finding 1). Each upsert
        takes a row lock on ``audit_sequence`` that is held until the batch
        commits, so the order in which one flush issues them is the order it
        acquires locks. Issuing them per event in queue order meant worker A
        could hold ``c1`` and wait for ``c2`` while worker B held ``c2`` and
        waited for ``c1``: PostgreSQL aborted one of them and that whole batch
        was lost. With every flush in every process acquiring in one global
        order, a later flush queues behind an earlier one instead of forming
        a cycle. One upsert per correlation (not per event) also means each
        row is locked exactly once per flush.

        Within one correlation_id, the reserved range is handed out in batch
        (queue) order, so per-correlation event order is preserved.
        """
        counts = Counter(event.correlation_id for event in batch)
        next_in_range: dict[str, int] = {}
        for correlation_id in sorted(counts):
            count = counts[correlation_id]
            result = await session.execute(
                _RESERVE_SEQUENCE_RANGE_SQL,
                {"correlation_id": correlation_id, "count": count},
            )
            last_reserved: int = result.scalar_one()
            next_in_range[correlation_id] = last_reserved - count + 1

        sequences: list[int] = []
        for event in batch:
            sequences.append(next_in_range[event.correlation_id])
            next_in_range[event.correlation_id] += 1
        return sequences

    async def _flush_batch(self, batch: list[_AuditEventBase]) -> None:
        """Open one AsyncSession, add all events, commit once, close (REQ-AUDIT-33).

        Each event's ``sequence`` (a recorder-time placeholder — see
        ``recorder.py``) is overwritten here with the real, authoritative
        value reserved by ``_reserve_sequences`` on THIS SAME session, so the
        reservation commits or rolls back with the rest of the batch — a
        failed batch releases its reserved numbers for free.
        """
        from agents_system.models.audit_event import map_to_audit_event

        try:
            async with self._session_factory() as session:
                sequences = await self._reserve_sequences(session, batch)
                for event, sequence in zip(batch, sequences, strict=True):
                    event_data = event.model_dump()
                    event_data["sequence"] = sequence
                    orm_row = map_to_audit_event(event_data)
                    session.add(orm_row)  # synchronous: awaiting None raises
                await session.commit()
        except Exception as exc:
            # Drainer must NEVER crash — log and continue.
            # The whole batch is lost, so it counts toward dropped_count: the
            # aggregate audit-loss signal (ADR-001 D-046) must not see only
            # queue overflow (PR #72 review, finding 4).
            self.dropped_count += len(batch)
            # `exc` must be bound: `str(Exception())` constructs a fresh empty
            # exception and stringifies THAT, so the only record of a failed
            # audit write carried an empty string.
            logger.exception(
                "audit.drain_failed",
                batch_size=len(batch),
                dropped_count=self.dropped_count,
                error=str(exc),
            )
