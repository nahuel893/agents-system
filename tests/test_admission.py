"""Unit tests for TurnAdmissionLimiter (#46 admission control primitive).

Covers acceptance criteria (a) and (c) from the issue at the primitive
level: the bound is enforced even under real concurrent load (mutation-
provable via events, no sleeps-as-sync), and the setting is validated.
Integration with the webhook worker (claim bounded to available slots, no
lease burnt at capacity, shutdown behaviour) lives in test_webhook_worker.py.
"""

from __future__ import annotations

import asyncio

import pytest

from agents_system.services.admission import (
    DEFAULT_MAX_CONCURRENT_TURNS,
    TurnAdmissionLimiter,
)


@pytest.mark.parametrize("max_concurrent_turns", [0, -1])
def test_rejects_non_positive_max_concurrent_turns(max_concurrent_turns: int) -> None:
    with pytest.raises(ValueError, match="max_concurrent_turns must be >= 1"):
        TurnAdmissionLimiter(max_concurrent_turns)


def test_default_conservative_default_is_positive() -> None:
    assert DEFAULT_MAX_CONCURRENT_TURNS >= 1


def test_try_acquire_and_available_track_capacity() -> None:
    limiter = TurnAdmissionLimiter(2)
    assert limiter.available == 2
    assert limiter.in_flight == 0

    assert limiter.try_acquire() is True
    assert limiter.available == 1
    assert limiter.in_flight == 1

    assert limiter.try_acquire() is True
    assert limiter.available == 0
    assert limiter.in_flight == 2

    # At capacity: try_acquire refuses without mutating state.
    assert limiter.try_acquire() is False
    assert limiter.available == 0
    assert limiter.in_flight == 2


async def test_release_without_a_matching_acquire_raises() -> None:
    limiter = TurnAdmissionLimiter(1)
    with pytest.raises(RuntimeError, match="no turn in flight"):
        await limiter.release()


async def test_acquire_blocks_until_a_slot_is_released() -> None:
    limiter = TurnAdmissionLimiter(1)
    await limiter.acquire()  # take the only slot

    waiter_acquired = asyncio.Event()

    async def waiter() -> None:
        await limiter.acquire()
        waiter_acquired.set()

    task = asyncio.create_task(waiter())
    try:
        # The single slot is held: the waiter must not have gotten in yet.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(waiter_acquired.wait(), timeout=0.05)
        assert limiter.in_flight == 1

        await limiter.release()

        # Now that a slot freed, the waiter is unblocked.
        await asyncio.wait_for(waiter_acquired.wait(), timeout=1)
        assert limiter.in_flight == 1
    finally:
        await asyncio.wait_for(task, timeout=1)
        await limiter.release()


async def test_never_admits_more_than_the_configured_limit_concurrently() -> None:
    """Mutation-provable (a): gather more callers than the limit and prove
    the observed peak concurrency never exceeds it, using events for
    deterministic synchronization -- no sleeps as a substitute for sync.

    Mutating ``try_acquire`` to always return True (i.e. removing the bound)
    makes every one of the 5 callers enter immediately, pushing ``peak`` to
    5 and turning this red.
    """
    limiter = TurnAdmissionLimiter(2)
    concurrent_callers = 5
    in_flight = 0
    peak = 0
    release_gate = asyncio.Event()
    entered = [asyncio.Event() for _ in range(concurrent_callers)]

    async def hold_a_slot(index: int) -> None:
        nonlocal in_flight, peak
        async with limiter.slot():
            in_flight += 1
            peak = max(peak, in_flight)
            entered[index].set()
            await release_gate.wait()
            in_flight -= 1

    tasks = [asyncio.create_task(hold_a_slot(i)) for i in range(concurrent_callers)]
    try:
        await asyncio.wait_for(
            asyncio.gather(entered[0].wait(), entered[1].wait()), timeout=1
        )
        assert peak == 2
        assert not any(e.is_set() for e in entered[2:]), (
            "more callers were admitted than the configured limit"
        )

        release_gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()

    assert peak == 2, "observed concurrency exceeded the configured limit"
    assert limiter.in_flight == 0
    assert limiter.available == 2
