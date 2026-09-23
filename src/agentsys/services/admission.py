"""Process-wide admission control for concurrent agent turns (#46).

Nothing previously bounded how many turns could run at once. Two entry
points run a turn: the webhook worker's poll loop
(``services/webhook_worker.py``) and the OpenAI-compatible
``POST /v1/chat/completions`` adapter (``integration/openai_adapter.py``),
which calls ``AgentRuntime.run_turn`` directly, in-request, once per HTTP
call. Neither had a limit -- a burst of arriving conversations opened one
turn per conversation and could exhaust the database connection pool
regardless of process count (ADR-001 D-033).

``main.py``'s lifespan constructs exactly one ``TurnAdmissionLimiter`` (sized
from ``Settings.max_concurrent_turns``) and shares it, via ``app.state``,
between both entry points -- the bound is process-wide, not per-caller.

The approved decision (see ``odd/tasks/durable-webhook-delivery.md``, W3) is
backpressure, never rejection: at capacity, already-durably-persisted
webhook work simply waits, unclaimed, in the outbox; it is never dropped or
answered with an error. That is why this is not a bare ``asyncio.Semaphore``:
the webhook worker needs to know how many slots are free *before* it claims
outbox work, so claiming itself stays capped at what it could actually run --
at capacity it claims nothing, so no lease is taken and no attempt is spent
on work it cannot start yet.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

# Matches ADR-001's Stage A launch target ("10 concurrent conversations: a
# single process") -- D-033 explicitly calls the bound "needed at 10, not
# just at 100". A turn holds AT MOST ONE database connection at a time --
# never one per tool call, since agent/graph.py::_execute_tools runs its
# tool_calls sequentially over one shared AsyncSession per round, not one
# session per call -- so 10 concurrent turns implies at most 10 held
# connections from this path at once, well under a single engine's default
# pool ceiling of 15 (pool_size=5 + max_overflow=10,
# `models/base.py::get_engine`). That bounds how MANY connections are held
# at once, not how LONG any one of them is held: a round with several slow
# sequential tool calls holds its one connection for the round's full
# cumulative latency. #45 owns tuning the pool itself; this default is
# chosen to already fit it, not to anticipate it.
DEFAULT_MAX_CONCURRENT_TURNS = 10


class TurnAdmissionLimiter:
    """Bounds how many turns may execute concurrently across the process."""

    def __init__(self, max_concurrent_turns: int) -> None:
        if max_concurrent_turns < 1:
            raise ValueError("max_concurrent_turns must be >= 1")
        self._max_concurrent_turns = max_concurrent_turns
        self._in_flight = 0
        self._condition = asyncio.Condition()

    @property
    def max_concurrent_turns(self) -> int:
        return self._max_concurrent_turns

    @property
    def in_flight(self) -> int:
        """Turns currently holding a slot."""
        return self._in_flight

    @property
    def available(self) -> int:
        """Slots free right now.

        Meant for an admission decision made *before* claiming durable work
        (see module docstring). Reading it outside ``_condition``'s lock is
        deliberate and safe: ``try_acquire``/``acquire`` re-check the real
        bound atomically at acquisition time, so a stale read here can only
        under-claim, never let more than ``max_concurrent_turns`` turns run.
        """
        return max(0, self._max_concurrent_turns - self._in_flight)

    def try_acquire(self) -> bool:
        """Reserve one slot without blocking. False means at capacity.

        Synchronous and awaits nothing, so -- under asyncio's single-threaded
        cooperative scheduling -- the check and the increment are atomic with
        respect to every other coroutine.
        """
        if self._in_flight >= self._max_concurrent_turns:
            return False
        self._in_flight += 1
        return True

    async def acquire(self) -> None:
        """Block until a slot is free, then reserve it."""
        async with self._condition:
            await self._condition.wait_for(self.try_acquire)

    async def release(self) -> None:
        """Free one previously reserved slot and wake any waiters."""
        async with self._condition:
            if self._in_flight <= 0:
                raise RuntimeError("release() called with no turn in flight")
            self._in_flight -= 1
            self._condition.notify_all()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one admission slot for the duration of the ``async with`` block."""
        await self.acquire()
        try:
            yield
        finally:
            await self.release()
