"""Tests for AuditSink — bounded queue, drop-on-overflow, drainer batching (T-16..T-19)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from agentsys.audit.events import ToolCallAttempted
from agentsys.audit.sink import AuditSink
import uuid
from datetime import datetime, timezone


def make_event(sequence: int = 1, corr_id: str = "test-corr") -> ToolCallAttempted:
    """Create a real ToolCallAttempted event for testing."""
    return ToolCallAttempted(
        event_id=uuid.uuid4(),
        occurred_at=datetime.now(timezone.utc),
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

        with patch("agentsys.audit.sink.logger") as mock_logger:
            await sink.record(make_event())
            mock_logger.warning.assert_called_once()
            args, kwargs = mock_logger.warning.call_args
            assert "audit.event_dropped" in args or kwargs.get("event", "").startswith("audit.")


class _FakeSession:
    """A minimal fake async session that records events.

    ``add`` is SYNCHRONOUS because ``sqlalchemy.ext.asyncio.AsyncSession.add``
    is (verified: ``inspect.iscoroutinefunction`` returns False). It used to be
    ``async def`` here, which matched the sink's ``await session.add(...)`` —
    so the fake and the buggy code agreed with each other and disagreed with
    SQLAlchemy. Against a real session that await raises TypeError, the
    drainer's ``except Exception`` swallows it, and no audit row is ever
    written. A double that mirrors the defect tests the double.
    """

    def __init__(self) -> None:
        self.added: list = []
        self.commit_count = 0

    def add(self, event) -> None:
        self.added.append(event)

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
        from agentsys.audit.sink import _app_ctx

        # Isolate by clearing context
        _app_ctx.set({})

        with pytest.raises(RuntimeError, match="no sink is registered"):
            AuditSink.current()

    @pytest.mark.asyncio
    async def test_set_and_get_current(self):
        """AuditSink.set_current() + current() work as a singleton."""
        from agentsys.audit.sink import _app_ctx

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


class TestAuditSinkSequenceAllocation:
    """T-18/19: Sequence allocator gives distinct sequences per correlation_id."""

    @pytest.mark.asyncio
    async def test_sequence_distinct_per_correlation_id(self):
        """Two events for same correlation_id get distinct sequences."""
        from agentsys.audit.recorder import _seq_counter, _allocate_sequence

        # Clean slate for this test
        _seq_counter.clear()
        correlation_id = "test-seq-123"
        seq1 = await _allocate_sequence(correlation_id)
        seq2 = await _allocate_sequence(correlation_id)
        seq3 = await _allocate_sequence(correlation_id)

        assert seq2 == seq1 + 1
        assert seq3 == seq2 + 1
        assert seq1 != seq2 != seq3
        # Cleanup
        _seq_counter.clear()

    @pytest.mark.asyncio
    async def test_sequence_resets_for_new_correlation_id(self):
        """Different correlation_ids get independent sequences (each starts at 1)."""
        from agentsys.audit.recorder import _seq_counter, _allocate_sequence

        # Clean slate for this test
        _seq_counter.clear()
        # First allocation for each correlation_id starts at 1
        seq_a1 = await _allocate_sequence("corr-a")
        seq_b1 = await _allocate_sequence("corr-b")
        assert seq_a1 == 1
        assert seq_b1 == 1  # Both start at 1 independently
        # Second allocation for same correlation_id increments
        seq_a2 = await _allocate_sequence("corr-a")
        assert seq_a2 == seq_a1 + 1
        # Cleanup
        _seq_counter.clear()

    @pytest.mark.asyncio
    async def test_sequence_allocator_is_async_safe(self):
        """Concurrent sequence allocations are all distinct (async-safe)."""
        from agentsys.audit.recorder import _seq_counter, _allocate_sequence

        # Clean slate for this test
        _seq_counter.clear()
        correlation_id = "test-parallel"

        async def allocate_many(n: int) -> list[int]:
            return [await _allocate_sequence(correlation_id) for _ in range(n)]

        # Allocate 50 concurrently
        results = await asyncio.gather(*[allocate_many(10) for _ in range(5)])
        all_seqs = [s for batch in results for s in batch]

        assert len(set(all_seqs)) == 50, "All sequences must be unique"
        assert sorted(all_seqs) == list(range(1, 51)), "Sequences 1..50 in order"
        # Cleanup
        _seq_counter.clear()
