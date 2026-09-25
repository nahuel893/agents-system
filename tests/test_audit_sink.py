"""Tests for AuditSink — bounded queue, drop-on-overflow, drainer batching (T-16..T-19)."""

from __future__ import annotations

import asyncio
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

    ``execute`` mimics the atomic per-correlation-id upsert
    (``audit_sequence``, migration 005) that ``_flush_batch`` now issues
    before mapping each event: an in-memory dict standing in for the real
    table's row-locked ``next_seq`` counter, so sequences it hands out are
    increasing per ``correlation_id`` and independent across them — the same
    contract the real upsert holds, minus the cross-process guarantee (which
    only a real PostgreSQL round trip can prove; see
    ``tests/test_audit_migration_integration.py``).
    """

    def __init__(self) -> None:
        self.added: list = []
        self.commit_count = 0
        self.executed: list[tuple[Any, dict]] = []
        self._next_seq: dict[str, int] = {}

    def add(self, event) -> None:
        self.added.append(event)

    async def execute(
        self, statement: Any, params: dict | None = None
    ) -> _FakeExecuteResult:
        params = params or {}
        self.executed.append((statement, params))
        correlation_id = params["correlation_id"]
        next_seq = self._next_seq.get(correlation_id, 0) + 1
        self._next_seq[correlation_id] = next_seq
        return _FakeExecuteResult(next_seq)

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
        sink = AuditSink(session_factory=MagicMock(), maxsize=1000)

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
    async def test_flush_batch_issues_the_atomic_upsert_per_event(self):
        """Every event in the batch triggers one upsert, keyed by its correlation_id."""
        fake_session = _FakeSession()

        def fake_factory():
            return fake_session

        sink = AuditSink(session_factory=fake_factory, maxsize=10)

        await sink._flush_batch(
            [make_event(corr_id="corr-a"), make_event(corr_id="corr-a")]
        )

        assert len(fake_session.executed) == 2
        for _statement, params in fake_session.executed:
            assert params == {"correlation_id": "corr-a"}

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
