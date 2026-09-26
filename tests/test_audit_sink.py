"""Tests for AuditSink — bounded queue, drop-on-overflow, drainer batching (T-16..T-19)."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from agents_system.audit.events import ToolCallAttempted
from agents_system.audit.sink import AuditSink


def make_event(sequence: int = 1, corr_id: str = "test-corr") -> ToolCallAttempted:
    """Create a real ToolCallAttempted event for testing."""
    return ToolCallAttempted(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(UTC),
        correlation_id=corr_id,
        sequence=sequence,
        role="test-role",
        tool_name="test_tool",
        payload={"tool_name": "test_tool"},
        pii_keys=[],
    )


class TestAuditSinkQueueOverflow:
    """T-16 RED: Queue overflow drops 1001st event without raising (REQ-AUDIT-31)."""

    @pytest.mark.asyncio
    async def test_record_1001st_event_does_not_raise(self):
        """Given the queue is full, the 1001st record() call must NOT raise."""
        sink = AuditSink(session_factory=MagicMock(), maxsize=10)
        sink._started = True  # bypass start-check

        for _ in range(10):
            await sink.record(make_event())

        # The 11th call should not raise
        try:
            await sink.record(make_event())
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"record() raised unexpectedly: {exc}")

    @pytest.mark.asyncio
    async def test_overflow_increments_dropped_count(self):
        """Given the queue is full, dropped_count increments."""
        sink = AuditSink(session_factory=MagicMock(), maxsize=5)
        sink._started = True

        for _ in range(5):
            await sink.record(make_event())

        initial_count = sink.dropped_count
        await sink.record(make_event())  # overflow
        assert sink.dropped_count == initial_count + 1

    @pytest.mark.asyncio
    async def test_overflow_logs_warning(self):
        """Given the queue is full, a warning is logged."""
        sink = AuditSink(session_factory=MagicMock(), maxsize=3)
        sink._started = True

        for _ in range(3):
            await sink.record(make_event())

        with patch("agents_system.audit.sink.logger") as mock_logger:
            await sink.record(make_event())
            mock_logger.warning.assert_called_once()
            args, kwargs = mock_logger.warning.call_args
            assert "audit.event_dropped" in args or kwargs.get("event", "").startswith(
                "audit."
            )


class _FakeExecuteResult:
    """Stands in for the ``Result`` of ``session.execute(_ALLOCATE_SEQUENCE_SQL, ...)``.

    Only ``scalar_one()`` is used by ``_flush_batch`` — the atomic upsert's
    ``RETURNING next_seq`` gives back exactly one row, one column.
    """

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar_one(self) -> int:
        return self._value


class _FakeSession:
    """A minimal fake async session that records events.

    ``add`` is SYNCHRONOUS because ``sqlalchemy.ext.asyncio.AsyncSession.add``
    is (verified: ``inspect.iscoroutinefunction`` returns False). It used to be
    ``async def`` here, which matched the sink's ``await session.add(...)`` —
    so the fake and the buggy code agreed with each other and disagreed with
    SQLAlchemy. Against a real session that await raises TypeError, the
    drainer's ``except Exception`` swallows it, and no audit row is ever
    written. A double that mirrors the defect tests the double.

    ``execute`` mimics the range-reserving upsert against ``audit_sequence``
    (migration 005) that ``_flush_batch`` issues once per distinct
    ``correlation_id``: ``count`` values are reserved at once and the LAST
    one is returned, exactly like ``next_seq = next_seq + EXCLUDED.next_seq
    RETURNING next_seq``. An in-memory dict stands in for the real table's
    row-locked counter -- the same contract minus the cross-process and
    lock-ordering guarantees, which only a real PostgreSQL round trip can
    prove (see ``tests/test_audit_migration_integration.py``).
    """

    def __init__(self, next_seq: dict[str, int] | None = None) -> None:
        self.added: list = []
        self.commit_count = 0
        self.executed: list[tuple[Any, dict]] = []
        self._next_seq: dict[str, int] = dict(next_seq or {})

    def add(self, event) -> None:
        self.added.append(event)

    async def execute(
        self, statement: Any, params: dict | None = None
    ) -> _FakeExecuteResult:
        params = params or {}
        self.executed.append((statement, params))
        correlation_id = params["correlation_id"]
        last_reserved = self._next_seq.get(correlation_id, 0) + params["count"]
        self._next_seq[correlation_id] = last_reserved
        return _FakeExecuteResult(last_reserved)

    async def commit(self) -> None:
        self.commit_count += 1

    async def close(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class TestAuditSinkDrainer:
    """T-16/17: Drainer flushes every 100ms OR when 50 events accumulate."""

    @pytest.mark.asyncio
    async def test_drainer_flushes_on_batch_size(self):
        """Given 50 events accumulated, drainer flushes without waiting for timeout."""
        # A real-shaped fake, not MagicMock(): the real _flush_batch runs here,
        # and a MagicMock session hands back an AsyncMock ``add`` whose
        # never-awaited coroutine surfaced as a RuntimeWarning (PR #72 review,
        # finding 7). ``AsyncSession.add`` is synchronous; so is the fake's.
        fake_session = _FakeSession()
        sink = AuditSink(session_factory=lambda: fake_session, maxsize=1000)

        flush_started = asyncio.Event()
        flush_done = asyncio.Event()
        orig_flush = sink._flush_batch

        async def tracking_flush(batch):
            flush_started.set()
            await orig_flush(batch)
            flush_done.set()

        sink._flush_batch = tracking_flush
        await sink.start()

        # Add 50 events
        for i in range(50):
            await sink.record(make_event(sequence=i))

        # Wait for flush to complete (proves drainer processed the batch)
        await asyncio.wait_for(flush_started.wait(), timeout=1.0)
        await asyncio.wait_for(flush_done.wait(), timeout=1.0)

        await sink.stop()
        assert len(fake_session.added) == 50

    @pytest.mark.asyncio
    async def test_drainer_flushes_on_timeout(self):
        """Given fewer than 50 events, drainer flushes after 100ms."""
        sink = AuditSink(session_factory=MagicMock(), maxsize=1000)

        flushed = asyncio.Event()

        async def tracking_flush(batch):
            flushed.set()

        sink._flush_batch = tracking_flush
        await sink.start()

        # Add 5 events (below batch threshold)
        for i in range(5):
            await sink.record(make_event(sequence=i))

        # Wait for flush (should happen on 100ms timeout)
        await asyncio.wait_for(flushed.wait(), timeout=0.5)

        await sink.stop()

    @pytest.mark.asyncio
    async def test_drainer_uses_single_transaction(self):
        """All events in a batch use ONE session transaction (REQ-AUDIT-33)."""
        fake_session = _FakeSession()

        def fake_factory():
            return fake_session

        sink = AuditSink(session_factory=fake_factory, maxsize=1000)
        await sink.start()

        for i in range(20):
            await sink.record(make_event(sequence=i))

        # Wait for flush
        await asyncio.sleep(0.25)

        # Should be one commit for the whole batch
        assert fake_session.commit_count == 1
        await sink.stop()


class TestAuditSinkStartStop:
    """T-16/17: start() spawns drainer; stop() drains queue then cancels."""

    @pytest.mark.asyncio
    async def test_record_raises_if_not_started(self):
        """Given sink.start() was not called, record() raises RuntimeError."""
        sink = AuditSink(session_factory=MagicMock())

        with pytest.raises(RuntimeError, match="before start"):
            await sink.record(make_event())

    @pytest.mark.asyncio
    async def test_start_spawns_drainer(self):
        """Given start() is called, the drainer task exists."""
        sink = AuditSink(session_factory=MagicMock())
        assert not hasattr(sink, "_drainer_task") or sink._drainer_task is None

        await sink.start()
        await asyncio.sleep(0.05)  # allow task to spawn

        assert hasattr(sink, "_drainer_task")
        assert sink._drainer_task is not None
        assert not sink._drainer_task.done()

        await sink.stop()

    @pytest.mark.asyncio
    async def test_stop_drains_queue(self):
        """Given stop() is called while queue has events, drain() flushes all."""
        # Use a pre-started sink but override _flush_batch to track calls
        fake_session = _FakeSession()

        def fake_factory():
            return fake_session

        sink = AuditSink(session_factory=fake_factory, maxsize=1000)
        await sink.start()

        # Add events directly to queue (bypass the drainer's running loop)
        for i in range(10):
            sink._queue.put_nowait(make_event(sequence=i))

        # Now call drain — this should flush all 10
        await sink.drain()

        # drain() does NOT cancel the drainer; cancel it explicitly
        sink._shutdown = True
        if sink._drainer_task:
            sink._drainer_task.cancel()
            try:
                await sink._drainer_task
            except asyncio.CancelledError:
                pass

        # All 10 events should have been flushed via drain()
        assert len(fake_session.added) == 10

    @pytest.mark.asyncio
    async def test_stop_cancels_drainer(self):
        """Given stop() completes, the drainer task is done."""
        sink = AuditSink(session_factory=MagicMock())
        await sink.start()
        await asyncio.sleep(0.05)

        await sink.stop()
        assert sink._drainer_task.done()


class TestAuditSinkCurrent:
    """AuditSink.current() returns the singleton from app.state."""

    def test_current_raises_when_not_registered(self):
        """AuditSink.current() raises RuntimeError when no sink is registered."""
        from agents_system.audit.sink import _app_ctx

        # Isolate by clearing context
        _app_ctx.set({})

        with pytest.raises(RuntimeError, match="no sink is registered"):
            AuditSink.current()

    @pytest.mark.asyncio
    async def test_set_and_get_current(self):
        """AuditSink.set_current() + current() work as a singleton."""
        from agents_system.audit.sink import _app_ctx

        _app_ctx.set({})

        mock_factory = MagicMock()
        sink = AuditSink(session_factory=mock_factory)
        AuditSink.set_current(sink)

        assert AuditSink.current() is sink


class TestAuditSinkSlowDB:
    """T-18/19: Slow DB drainer does not block interceptor p95 latency."""

    @pytest.mark.asyncio
    async def test_record_returns_quickly_despite_slow_db(self):
        """record() must return within 50ms even if DB commit is slow."""

        class SlowDBSink(AuditSink):
            async def _flush_batch(self, batch):
                await asyncio.sleep(0.05)  # 50ms delay
                # Don't actually commit

        sink = SlowDBSink(session_factory=MagicMock(), maxsize=1000)
        await sink.start()

        event = make_event()

        start = time.perf_counter()
        await sink.record(event)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert elapsed_ms < 50, f"record() took {elapsed_ms:.1f}ms, expected < 50ms"
        await sink.stop()


class TestAuditSinkFlushBatchSequenceAllocation:
    """Issue #9: the authoritative sequence is allocated at flush time, in the DB.

    The old `_allocate_sequence`/`_seq_counter` (a module-level dict guarded
    by an `asyncio.Lock`) was a per-PROCESS counter: correct within one
    process, wrong across N, because every process counts the `"none"`
    fallback `correlation_id` from 1 independently — two workers emitting a
    contextless event in the same instant could allocate the same sequence
    and collide on `uq_audit_event_correlation_sequence`. Those symbols are
    gone; `recorder.py`'s `record_*` helpers now write a documented
    placeholder, and `AuditSink._flush_batch` allocates the real value,
    atomically, per event, via an upsert against `audit_sequence` (migration
    005) run on the SAME session the batch commits with.

    These tests exercise `_flush_batch` against `_FakeSession`, whose
    `execute()` mimics that upsert's per-correlation_id counting contract.
    They cannot prove the upsert survives concurrent processes — only a real
    PostgreSQL round trip can, which is what
    `tests/test_audit_migration_integration.py::TestAuditSequenceAllocationIsProcessSafe`
    is for.
    """

    @pytest.mark.asyncio
    async def test_flush_batch_reserves_one_range_per_correlation_in_sorted_order(
        self,
    ):
        """One upsert per DISTINCT correlation_id, in sorted order, sized to it.

        PR #72 review, finding 1 (CRITICAL): one upsert per event in FIFO
        order made every flush take its ``audit_sequence`` row locks in its
        own arrival order, so two workers whose batches named the same
        correlation_ids in opposite orders deadlocked, and PostgreSQL's
        victim batch was dropped. Sorting is the global lock order that makes
        a cycle impossible; reserving ``count`` at once is what lets each
        row be locked exactly once per flush.
        """
        fake_session = _FakeSession()
        sink = AuditSink(session_factory=lambda: fake_session, maxsize=10)

        await sink._flush_batch(
            [
                make_event(corr_id="corr-b"),
                make_event(corr_id="corr-a"),
                make_event(corr_id="corr-b"),
                make_event(corr_id="corr-c"),
                make_event(corr_id="corr-a"),
                make_event(corr_id="corr-b"),
            ]
        )

        assert [params for _statement, params in fake_session.executed] == [
            {"correlation_id": "corr-a", "count": 2},
            {"correlation_id": "corr-b", "count": 3},
            {"correlation_id": "corr-c", "count": 1},
        ]

    @pytest.mark.asyncio
    async def test_flush_batch_keeps_queue_order_within_each_correlation(self):
        """Interleaved correlations: rows are added in queue order, and each
        correlation's reserved range is handed out in that same order."""
        fake_session = _FakeSession()
        sink = AuditSink(session_factory=lambda: fake_session, maxsize=10)
        batch = [
            make_event(corr_id="corr-b"),
            make_event(corr_id="corr-a"),
            make_event(corr_id="corr-b"),
            make_event(corr_id="corr-a"),
            make_event(corr_id="corr-b"),
        ]

        await sink._flush_batch(batch)

        assert [row.event_id for row in fake_session.added] == [
            event.event_id for event in batch
        ]
        assert [(row.correlation_id, row.sequence) for row in fake_session.added] == [
            ("corr-b", 1),
            ("corr-a", 1),
            ("corr-b", 2),
            ("corr-a", 2),
            ("corr-b", 3),
        ]

    @pytest.mark.asyncio
    async def test_flush_batch_range_starts_after_the_stored_counter(self):
        """The returned value is the LAST reserved number: a counter already at
        10 plus a batch of 3 yields 11, 12, 13 -- never 10 again, never a gap."""
        fake_session = _FakeSession(next_seq={"corr-a": 10})
        sink = AuditSink(session_factory=lambda: fake_session, maxsize=10)

        await sink._flush_batch([make_event(corr_id="corr-a") for _ in range(3)])

        assert [row.sequence for row in fake_session.added] == [11, 12, 13]

    @pytest.mark.asyncio
    async def test_flush_batch_assigns_increasing_sequence_same_correlation(self):
        """Same-correlation events in one batch get 1, 2, 3, ... in FIFO order."""
        fake_session = _FakeSession()

        def fake_factory():
            return fake_session

        sink = AuditSink(session_factory=fake_factory, maxsize=10)

        await sink._flush_batch([make_event(corr_id="corr-b") for _ in range(3)])

        assert [row.sequence for row in fake_session.added] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_flush_batch_sequence_continues_across_flushes(self):
        """A later flush for the same correlation_id continues, never restarts."""
        fake_session = _FakeSession()

        def fake_factory():
            return fake_session

        sink = AuditSink(session_factory=fake_factory, maxsize=10)

        await sink._flush_batch([make_event(corr_id="corr-c")])
        await sink._flush_batch([make_event(corr_id="corr-c")])

        assert [row.sequence for row in fake_session.added] == [1, 2]

    @pytest.mark.asyncio
    async def test_flush_batch_sequence_independent_per_correlation(self):
        """Two different correlation_ids each start their own count at 1."""
        fake_session = _FakeSession()

        def fake_factory():
            return fake_session

        sink = AuditSink(session_factory=fake_factory, maxsize=10)

        await sink._flush_batch(
            [make_event(corr_id="corr-x"), make_event(corr_id="corr-y")]
        )

        by_corr = {row.correlation_id: row.sequence for row in fake_session.added}
        assert by_corr == {"corr-x": 1, "corr-y": 1}

    @pytest.mark.asyncio
    async def test_flush_batch_overwrites_the_recorder_placeholder(self):
        """The event's own `sequence` (a recorder-time placeholder) is discarded."""
        fake_session = _FakeSession()

        def fake_factory():
            return fake_session

        sink = AuditSink(session_factory=fake_factory, maxsize=10)

        placeholder_event = make_event(sequence=0, corr_id="corr-z")
        await sink._flush_batch([placeholder_event])

        assert fake_session.added[0].sequence == 1


class TestDrainFailureIsDiagnosable:
    """The drainer must never crash — but a swallowed failure has to say what failed.

    This path had no coverage at all, which is how ``error=str(Exception())``
    survived: it constructs a *new* empty exception and stringifies that, so the
    only record of a failed audit write carried an empty string. The write
    failed, the log fired, and the log said nothing.
    """

    @pytest.mark.asyncio
    async def test_flush_failure_logs_the_actual_exception(self):
        """A failing session surfaces its own message, not an empty string."""
        sentinel = 'asyncpg: relation "audit_event" does not exist'

        class _ExplodingSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def execute(self, statement, params=None):
                # The atomic sequence upsert succeeds; `add()` below is what
                # must fail, so this test still proves the ADD failure path.
                return _FakeExecuteResult(1)

            def add(self, row):
                raise RuntimeError(sentinel)

            async def commit(self):  # pragma: no cover - never reached
                raise AssertionError("commit must not be reached")

        sink = AuditSink(session_factory=lambda: _ExplodingSession())

        with patch("agents_system.audit.sink.logger") as mock_logger:
            # Must not raise: the drainer's contract is that it never crashes.
            await sink._flush_batch([make_event()])

        assert mock_logger.exception.called, "a failed flush must be logged"
        _, kwargs = mock_logger.exception.call_args

        reported = str(kwargs.get("error", ""))
        assert reported, "the log recorded an empty error string"
        assert sentinel in reported, (
            f"the log must carry the real failure, got {reported!r}"
        )
        assert kwargs.get("batch_size") == 1

    @pytest.mark.asyncio
    async def test_flush_failure_counts_the_whole_batch_as_dropped(self):
        """PR #72 review, finding 4: a failed flush loses every event in it.

        ADR-001 D-046 makes ``dropped_count`` the aggregate audit-loss signal.
        It used to move only on queue overflow, so a deadlock victim or a
        dropped connection -- a whole batch gone -- left it untouched.
        """

        class _CommitFails(_FakeSession):
            async def commit(self) -> None:
                raise RuntimeError("deadlock detected")

        sink = AuditSink(session_factory=lambda: _CommitFails())

        with patch("agents_system.audit.sink.logger") as mock_logger:
            await sink._flush_batch([make_event() for _ in range(3)])

        assert sink.dropped_count == 3
        _, kwargs = mock_logger.exception.call_args
        assert kwargs.get("batch_size") == 3
        assert kwargs.get("dropped_count") == 3


class _StuckFlushSink(AuditSink):
    """Real queue and drainer; ``_flush_batch`` never returns until cancelled."""

    def __init__(self) -> None:
        super().__init__(session_factory=MagicMock(), maxsize=100)
        self.flush_started = asyncio.Event()

    async def _flush_batch(self, batch: list[Any]) -> None:
        self.flush_started.set()
        await asyncio.Event().wait()  # a wedged DB call


class _CapturingSink(AuditSink):
    """Real queue and drainer; the flush is slow enough to stop() during it."""

    def __init__(self) -> None:
        super().__init__(session_factory=MagicMock(), maxsize=100)
        self.captured: list[Any] = []
        self.flush_started = asyncio.Event()

    async def _flush_batch(self, batch: list[Any]) -> None:
        self.flush_started.set()
        await asyncio.sleep(0.05)
        self.captured.extend(batch)


class TestAuditSinkShutdownAccounting:
    """Every event handed to the sink is either persisted or counted as dropped.

    PR #72 review, findings 3 and 5. ``dropped_count`` is the only aggregate
    loss signal (ADR-001 D-046), so an event that is neither written nor
    counted is invisible loss.
    """

    @pytest.mark.asyncio
    async def test_stop_timeout_cancels_the_drainer_and_counts_every_lost_event(
        self,
    ):
        """Finding 5: the timeout branch had no test at all.

        A wedged flush holds 3 events the drainer already took off the queue;
        2 more are still queued behind it. All 5 are lost when the drainer is
        cancelled -- counting only ``qsize()`` reported 2.
        """
        sink = _StuckFlushSink()
        await sink.start()
        for _ in range(3):
            await sink.record(make_event())
        await asyncio.wait_for(sink.flush_started.wait(), timeout=1.0)
        for _ in range(2):
            await sink.record(make_event())

        with patch("agents_system.audit.sink.logger") as mock_logger:
            await sink.stop(timeout=0.05)

        assert sink._drainer_task is not None
        assert sink._drainer_task.cancelled()
        assert sink.dropped_count == 5
        mock_logger.warning.assert_called_once_with(
            "audit.shutdown_timeout",
            timeout_s=0.05,
            queued_events_dropped=2,
            in_flight_events_dropped=3,
            dropped_count=5,
        )
        assert sink._queue.empty()

    @pytest.mark.asyncio
    async def test_record_after_stop_is_dropped_and_counted_not_stranded(self):
        """Finding 3: after stop() nothing drains the queue any more.

        ``record()`` used to enqueue anyway, so the event sat in memory until
        the process exited, never written and never counted.
        """
        sink = _CapturingSink()
        await sink.start()
        await sink.stop()

        with patch("agents_system.audit.sink.logger") as mock_logger:
            await sink.record(make_event())  # must not raise

        assert sink.dropped_count == 1
        assert sink._queue.empty()
        mock_logger.warning.assert_called_once()
        args, kwargs = mock_logger.warning.call_args
        assert args == ("audit.event_dropped_shutdown",)
        assert kwargs["dropped_count"] == 1

    @pytest.mark.asyncio
    async def test_record_while_stop_is_draining_is_still_written(self):
        """Finding 3, the other side: only the final sweep closes the sink.

        stop() has begun but the drainer is still mid-flush, so its final
        sweep is still ahead of it -- an event recorded now is swept and
        written. Rejecting from the moment stop() starts would turn it into
        a needless (if counted) loss; the stranding the review found only
        exists AFTER that sweep, which is where record() starts rejecting.
        """
        sink = _CapturingSink()
        await sink.start()
        await sink.record(make_event())
        await asyncio.wait_for(sink.flush_started.wait(), timeout=1.0)

        stopping = asyncio.create_task(sink.stop())
        await asyncio.sleep(0)  # stop() has flagged shutdown; drainer still busy
        assert sink._shutdown
        await sink.record(make_event())
        await stopping

        assert len(sink.captured) == 2
        assert sink.dropped_count == 0
        assert sink._queue.empty()

    @pytest.mark.asyncio
    async def test_stop_counts_stragglers_a_dead_drainer_left_queued(self):
        """Finding 3: stop() sweeps and counts what no drainer will ever flush.

        Here the drainer died before stop() (cancelled from outside); the two
        events queued behind it are counted instead of silently left behind.
        """
        sink = _CapturingSink()
        await sink.start()
        assert sink._drainer_task is not None
        sink._drainer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sink._drainer_task
        await sink.record(make_event())
        await sink.record(make_event())

        with patch("agents_system.audit.sink.logger") as mock_logger:
            await sink.stop()

        assert sink.dropped_count == 2
        assert sink._queue.empty()
        mock_logger.warning.assert_called_once_with(
            "audit.shutdown_events_dropped",
            queued_events_dropped=2,
            in_flight_events_dropped=0,
            dropped_count=2,
        )

    @pytest.mark.asyncio
    async def test_clean_stop_drops_nothing_and_logs_nothing(self):
        """The happy path stays silent: everything queued is flushed."""
        sink = _CapturingSink()
        await sink.start()
        for _ in range(4):
            await sink.record(make_event())

        with patch("agents_system.audit.sink.logger") as mock_logger:
            await sink.stop()

        assert len(sink.captured) == 4
        assert sink.dropped_count == 0
        mock_logger.warning.assert_not_called()
