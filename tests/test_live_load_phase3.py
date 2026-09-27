"""Live-load: Phase 3 -- operation under concurrent load (#83, live-test
plan Phase 3, `docs/delivery/live-test-plan.md`).

Two layers, both driven through the REAL webhook -> `DeferredWebhookWorker`
path (never `AgentRuntime.run_turn` directly), via `boot_live_harness` (#78):

- A deterministic layer (`pytest.mark.load`): a fast, concurrency-safe fake
  model (`_LoadTestFakeModel`) drives 20/50/100 concurrent conversations
  through the real admission limiter, outbox, and Postgres connection pool --
  no network call and no model non-determinism, so every assertion here is a
  hard pass/fail, never a gated success rate the way Phase 1's happy-path
  scenarios are.
- A small real-model layer (`pytest.mark.load` AND `pytest.mark.live`): 20
  concurrent conversations through a real provider (`EVAL_PROVIDER`),
  reporting tokens/cost/latency -- proof the same load path holds up with a
  real model in the loop, not just the deterministic fake one.

Run with:
  `pytest -m load tests/test_live_load_phase3.py -v -s`
      (fake model, fast, needs only Postgres + Redis)
  `EVAL_PROVIDER=openai_compatible pytest -m 'load and live' \\
      tests/test_live_load_phase3.py -v -s`
      (real model, needs EVAL_PROVIDER + its own env vars)

See `docs/platform/live-eval.md`'s "Phase 3 load scenarios" section for
infrastructure requirements and how to read each scenario's printed report.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any, Self

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy.pool import QueuePool

from agents_system.config import get_settings
from agents_system.evals.live_harness import (
    LiveHarness,
    boot_live_harness,
    metric_value,
)
from agents_system.evals.provider import build_eval_model
from agents_system.services.outbox import MAX_OUTBOX_ATTEMPTS

pytestmark = pytest.mark.load

#: Reserved, per #107's sizing rule (`services/admission.py`'s own module
#: docstring; `Settings.__init__`'s own validator), for the in-process
#: AuditSink drainer and DeferredWebhookWorker, which share the operational
#: engine with every admitted turn: `database_pool_size +
#: database_max_overflow` must be at least `max_concurrent_turns` plus this
#: many.
_RESERVED_BACKGROUND_CONNECTIONS = 2

#: How often the background samplers below poll the admission limiter /
#: connection pool while a round is in flight. Small enough that a
#: millisecond-scale spike (all turns admitted by one poll cycle, running
#: concurrently against a real Postgres round trip each) is very unlikely to
#: fall entirely between two samples.
_SAMPLE_INTERVAL_S = 0.01


class _LoadTestFakeModel(FakeMessagesListChatModel):
    """Deterministic, concurrency-safe fake chat model (#83).

    Always answers with the SAME fixed `AIMessage`: a single-element
    `responses` list is never actually "cycled" --
    `FakeMessagesListChatModel._generate` resets its index back to `0` after
    every call whenever there is only one response -- so a conversation
    completes in exactly one model call regardless of how many OTHER
    conversations call this same model instance at the same time. `bind_tools`
    must not raise (`AgentRuntime.__init__` calls it whenever the role has any
    tools), and this fake never calls one, so every turn ends immediately
    after the first model response.
    """

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> _LoadTestFakeModel:
        return self


def _load_test_model() -> _LoadTestFakeModel:
    return _LoadTestFakeModel(
        responses=[
            AIMessage(
                content="Thanks, we'll follow up shortly.",
                usage_metadata={
                    "input_tokens": 42,
                    "output_tokens": 8,
                    "total_tokens": 50,
                },
            )
        ]
    )


def _percentile(values: Sequence[float], rank: float) -> float:
    """Nearest-rank percentile *rank* (0-100) of *values*.

    `statistics.quantiles` requires at least two data points; a single value
    is trivially its own every percentile.
    """
    if len(values) == 1:
        return values[0]
    quantiles = statistics.quantiles(sorted(values), n=100, method="inclusive")
    index = max(0, min(99, round(rank) - 1))
    return quantiles[index]


def _phone(tag: str, index: int) -> str:
    """A unique, opaque conversation identifier for one load scenario's
    fixture conversation -- `AcceptAllParticipantDirectory` accepts any
    well-formed non-empty address, so this need not be a plausible real
    phone number, only unique and stable per *tag*/*index*."""
    return f"+1{tag}{index:05d}"


async def _deliver_and_measure(
    harness: LiveHarness, *, phone: str, text: str, timeout: float = 60.0
) -> float:
    """POST one inbound webhook message and return the wall-clock seconds
    until its reply appears in the fake WhatsApp server's `sent` list --
    the full latency a real customer would observe, start to reply."""
    start = time.monotonic()
    response = await harness.deliver_webhook(phone=phone, text=text)
    assert response.status_code == 200, response.text
    await harness.wait_until(
        lambda: any(item.get("to") == phone for item in harness.outbound.sent),
        timeout=timeout,
    )
    return time.monotonic() - start


class _PeakSampler:
    """Samples one or more zero-argument readers on a fixed interval while
    a load round runs, keeping only each reader's peak value.

    A plain background `asyncio.Task` rather than a fixture: each round in
    the tests below needs its own start/stop bracketing a specific
    `asyncio.gather` call, not a whole test's lifetime.
    """

    def __init__(self, **readers: Callable[[], int]) -> None:
        self._readers = readers
        self.peaks: dict[str, int] = dict.fromkeys(readers, 0)
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            for name, read in self._readers.items():
                self.peaks[name] = max(self.peaks[name], read())
            await asyncio.sleep(_SAMPLE_INTERVAL_S)

    async def __aenter__(self) -> Self:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        # One final synchronous reading after cancellation -- the round's own
        # `asyncio.gather` already awaited completion by the time `__aexit__`
        # runs, so this catches a peak the last sampling tick might have
        # missed by up to `_SAMPLE_INTERVAL_S`.
        for name, read in self._readers.items():
            self.peaks[name] = max(self.peaks[name], read())


# ---------------------------------------------------------------------------
# Backpressure at the admission limiter's ceiling -- under, at, and over
# (#83 acceptance criterion).
# ---------------------------------------------------------------------------


async def test_admission_limiter_backpressure_under_at_and_over_ceiling() -> None:
    """One boot, three sequential rounds -- 5 (under), 10 (at), and 20
    (over) concurrent conversations -- against the deterministic fake model.

    `in_flight` must never exceed `max_concurrent_turns` in any round (the
    limiter's own invariant), nothing is ever dropped (every conversation
    still gets exactly one reply), and the over-ceiling round measurably
    takes longer than a single poll cycle -- proof that the excess work
    queued in the outbox and waited for a second poll to be claimed, rather
    than all running at once.
    """
    async with boot_live_harness(
        model=_load_test_model(), model_name="load-test-fake"
    ) as harness:
        limiter = harness.app.state.turn_admission_limiter
        assert limiter.max_concurrent_turns == 10
        poll_interval_s = get_settings().webhook_worker_poll_interval_s

        elapsed_by_round: dict[int, float] = {}
        for round_index, concurrency in enumerate((5, 10, 20)):
            phones = [_phone(f"bp{round_index}", i) for i in range(concurrency)]
            before_sent = len(harness.outbound.sent)

            async with _PeakSampler(in_flight=lambda: limiter.in_flight) as sampler:
                start = time.monotonic()
                await asyncio.gather(
                    *(
                        _deliver_and_measure(harness, phone=phone, text="hi")
                        for phone in phones
                    )
                )
                elapsed_by_round[concurrency] = time.monotonic() - start

            sent_this_round = len(harness.outbound.sent) - before_sent
            assert sent_this_round == concurrency, (
                f"round {concurrency}: expected exactly {concurrency} sends, "
                f"observed {sent_this_round} (a message was lost or "
                "duplicated)"
            )
            assert sampler.peaks["in_flight"] <= limiter.max_concurrent_turns, (
                f"round {concurrency}: observed in_flight peaked at "
                f"{sampler.peaks['in_flight']}, above the ceiling of "
                f"{limiter.max_concurrent_turns}"
            )
            print(
                f"  backpressure round concurrency={concurrency}: "
                f"peak in_flight={sampler.peaks['in_flight']} "
                f"(ceiling {limiter.max_concurrent_turns}), "
                f"elapsed={elapsed_by_round[concurrency]:.2f}s"
            )

        # Over the ceiling (20, double both max_concurrent_turns and the
        # worker's default claim_limit) needs at least a second poll cycle to
        # admit the remainder -- a real, measurable delay -- while 5 and 10
        # (at or under the ceiling) are claimed entirely on the first poll.
        assert elapsed_by_round[20] >= elapsed_by_round[10]
        assert elapsed_by_round[20] >= poll_interval_s * 0.5, (
            f"round 20 finished in {elapsed_by_round[20]:.2f}s, too fast for "
            f"a real second poll cycle (poll_interval_s={poll_interval_s}) -- "
            "backpressure may not actually be queuing the excess work"
        )


async def test_admission_limiter_is_shared_between_the_api_and_the_webhook() -> None:
    """The admission limiter is process-wide, shared by BOTH entry points
    (`services/admission.py`'s own module docstring) -- 10 conversations via
    the webhook and 10 via the OpenAI-compatible HTTP API
    (`send_chat_completion`, which holds its slot for the request's whole
    duration -- `integration/openai_adapter.py`), fired concurrently, must
    never let more than `max_concurrent_turns` (10) run at once COMBINED.
    Proves the cap is genuinely shared, not 10 per channel."""
    async with boot_live_harness(
        model=_load_test_model(), model_name="load-test-fake"
    ) as harness:
        limiter = harness.app.state.turn_admission_limiter
        assert limiter.max_concurrent_turns == 10
        webhook_phones = [_phone("mix", i) for i in range(10)]

        async with _PeakSampler(in_flight=lambda: limiter.in_flight) as sampler:
            webhook_latencies, api_responses = await asyncio.gather(
                asyncio.gather(
                    *(
                        _deliver_and_measure(harness, phone=phone, text="hi")
                        for phone in webhook_phones
                    )
                ),
                asyncio.gather(
                    *(
                        harness.send_chat_completion("Do you have Item Alpha in stock?")
                        for _ in range(10)
                    )
                ),
            )

        assert len(webhook_latencies) == 10
        for response in api_responses:
            assert response.status_code == 200, response.text

        assert sampler.peaks["in_flight"] <= limiter.max_concurrent_turns, (
            f"combined API + webhook load peaked in_flight at "
            f"{sampler.peaks['in_flight']}, above the shared ceiling of "
            f"{limiter.max_concurrent_turns} -- the limiter may not "
            "actually be shared across both entry points"
        )
        print(
            "  shared-admission mix (10 webhook + 10 API): peak in_flight="
            f"{sampler.peaks['in_flight']}/{limiter.max_concurrent_turns}"
        )


# ---------------------------------------------------------------------------
# 20 / 50 / 100 concurrent conversations: latency percentiles, memory, and
# pool usage against #107's sizing rule (#83 acceptance criteria).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("concurrency", [20, 50, 100])
async def test_load_reports_latency_percentiles_memory_and_pool_usage(
    concurrency: int,
) -> None:
    async with boot_live_harness(
        model=_load_test_model(), model_name="load-test-fake"
    ) as harness:
        settings = get_settings()
        limiter = harness.app.state.turn_admission_limiter
        pool = harness.engine.pool
        assert isinstance(pool, QueuePool), (
            f"expected the operational engine's default QueuePool, got "
            f"{type(pool).__name__} -- this scenario's pool-usage assertion "
            "needs checkedout()"
        )
        pool_ceiling = settings.database_pool_size + settings.database_max_overflow
        # #107's own rule, re-checked here against the settings this harness
        # actually booted with (not re-derived) -- Settings itself already
        # refuses to construct otherwise (test_config.py), so this is a
        # sanity echo, not a new invariant.
        assert pool_ceiling >= limiter.max_concurrent_turns + (
            _RESERVED_BACKGROUND_CONNECTIONS
        )

        before_metrics = await harness.scrape_metrics()
        memory_before = metric_value(before_metrics, "process_resident_memory_bytes")

        phones = [_phone("th", i) for i in range(concurrency)]
        async with _PeakSampler(
            in_flight=lambda: limiter.in_flight,
            pool_checked_out=pool.checkedout,
        ) as sampler:
            latencies = await asyncio.gather(
                *(
                    _deliver_and_measure(harness, phone=phone, text="hi")
                    for phone in phones
                )
            )

        after_metrics = await harness.scrape_metrics()
        memory_after = metric_value(after_metrics, "process_resident_memory_bytes")

        # Nothing lost or duplicated: exactly one send per conversation.
        to_counts = Counter(item["to"] for item in harness.outbound.sent)
        for phone in phones:
            assert to_counts[phone] == 1, (
                f"{phone}: expected exactly 1 send, observed {to_counts[phone]}"
            )

        assert sampler.peaks["in_flight"] <= limiter.max_concurrent_turns
        assert sampler.peaks["pool_checked_out"] <= pool_ceiling, (
            f"peak checked-out connections {sampler.peaks['pool_checked_out']} "
            f"exceeded the pool ceiling {pool_ceiling} (#107 sizing rule)"
        )

        p50 = _percentile(latencies, 50)
        p95 = _percentile(latencies, 95)
        p99 = _percentile(latencies, 99)
        print(
            f"load[{concurrency}]: "
            f"p50={p50 * 1000:.0f}ms p95={p95 * 1000:.0f}ms "
            f"p99={p99 * 1000:.0f}ms max={max(latencies) * 1000:.0f}ms "
            f"in_flight_peak={sampler.peaks['in_flight']}/"
            f"{limiter.max_concurrent_turns} "
            f"pool_checked_out_peak={sampler.peaks['pool_checked_out']}/"
            f"{pool_ceiling} "
            f"rss_before={memory_before} rss_after={memory_after}"
        )


# ---------------------------------------------------------------------------
# Per-conversation ordering under concurrent load (#83 acceptance criterion).
# ---------------------------------------------------------------------------


async def test_per_conversation_ordering_holds_under_concurrent_load() -> None:
    """8 messages for ONE conversation, fired concurrently alongside 24
    other conversations' worth of noise, must still complete in the same
    order they were enqueued in -- checked directly against the durable
    `outbox_work` rows (`LiveHarness.outbox_rows`), never against reply
    content (the fake model's reply never varies)."""
    async with boot_live_harness(
        model=_load_test_model(), model_name="load-test-fake"
    ) as harness:
        ordered_phone = _phone("ord", 0)
        noise_phones = [_phone("noise", i) for i in range(24)]
        message_count = 8

        responses = await asyncio.gather(
            *(
                harness.deliver_webhook(
                    phone=ordered_phone, text=f"seq-{i}", message_id=f"wamid.ord-{i}"
                )
                for i in range(message_count)
            ),
            *(
                harness.deliver_webhook(phone=phone, text="hi")
                for phone in noise_phones
            ),
        )
        for response in responses:
            assert response.status_code == 200, response.text

        # Polls the outbox rows' own `completed_at` directly, not
        # `outbound.sent`'s membership -- `process_claimed_work` appends to
        # `sent` (the WhatsApp send) BEFORE committing `complete_outbox_work`
        # (which sets `completed_at`), so waiting on `sent` alone races the
        # LAST message's commit under load.
        async def _all_ordered_messages_completed() -> bool:
            rows = await harness.outbox_rows(ordered_phone)
            return len(rows) == message_count and all(
                row.completed_at is not None for row in rows
            )

        await harness.wait_until(_all_ordered_messages_completed, timeout=90.0)

        rows = await harness.outbox_rows(ordered_phone)
        assert len(rows) == message_count
        assert all(row.completed_at is not None for row in rows), (
            "every message of the ordered conversation must have completed"
        )
        # `rows` is oldest-enqueued-first (`outbox_rows_statement`'s own
        # ordering); their completion times must be non-decreasing in that
        # SAME order -- a later-enqueued message must never finish before an
        # earlier one of the same conversation. The `is not None` check above
        # already guarantees every value here is a real `datetime`.
        completed_at = [
            row.completed_at for row in rows if row.completed_at is not None
        ]
        assert completed_at == sorted(completed_at), (
            f"per-conversation ordering violated: completion times "
            f"{completed_at} are not non-decreasing in enqueue order"
        )


# ---------------------------------------------------------------------------
# Retries and terminal outbox failures under load (#83 acceptance
# criterion).
# ---------------------------------------------------------------------------


async def test_retries_and_terminal_outbox_failure_observed_under_load() -> None:
    """One conversation is induced to fail twice then succeed (a retry);
    a second is induced to fail forever, past `MAX_OUTBOX_ATTEMPTS`, into a
    terminal failure -- both concurrently with a batch of ordinary
    conversations, so both paths are exercised UNDER load, not in
    isolation."""
    async with boot_live_harness(
        model=_load_test_model(), model_name="load-test-fake"
    ) as harness:
        retry_phone = _phone("retry", 0)
        terminal_phone = _phone("term", 0)
        noise_phones = [_phone("noise2", i) for i in range(15)]

        harness.outbound.fail_remaining[retry_phone] = 2
        harness.outbound.fail_remaining[terminal_phone] = MAX_OUTBOX_ATTEMPTS + 5

        await asyncio.gather(
            harness.deliver_webhook(phone=retry_phone, text="hi"),
            harness.deliver_webhook(phone=terminal_phone, text="hi"),
            *(
                harness.deliver_webhook(phone=phone, text="hi")
                for phone in noise_phones
            ),
        )

        # Retry path: 2 induced failures, then a real success. Polls the
        # outbox row's own `completed_at` directly, not `outbound.sent`'s
        # membership -- `process_claimed_work` appends to `sent` (the
        # WhatsApp send) BEFORE committing `complete_outbox_work` (which sets
        # `completed_at`), so waiting on `sent` alone races the commit under
        # load.
        async def _retry_row_is_completed() -> bool:
            rows = await harness.outbox_rows(retry_phone)
            return bool(rows) and rows[0].completed_at is not None

        await harness.wait_until(_retry_row_is_completed, timeout=90.0)
        retry_rows = await harness.outbox_rows(retry_phone)
        assert len(retry_rows) == 1
        assert retry_rows[0].attempt_count >= 3, (
            "expected at least 2 failed attempts plus 1 succeeding attempt"
        )
        assert retry_rows[0].completed_at is not None
        assert retry_rows[0].failed_at is None

        # Terminal path: always fails, terminalizes at MAX_OUTBOX_ATTEMPTS.
        async def _terminal_row_is_failed() -> bool:
            rows = await harness.outbox_rows(terminal_phone)
            return bool(rows) and rows[0].failed_at is not None

        await harness.wait_until(_terminal_row_is_failed, timeout=120.0)
        terminal_rows = await harness.outbox_rows(terminal_phone)
        assert len(terminal_rows) == 1
        assert terminal_rows[0].attempt_count == MAX_OUTBOX_ATTEMPTS
        assert terminal_rows[0].completed_at is None
        assert not any(
            item.get("to") == terminal_phone for item in harness.outbound.sent
        ), "a permanently failing send must never be recorded as sent"

        terminal_failure_events = await harness.audit_rows(
            event_type="webhook_delivery_terminal_failure"
        )
        assert any(
            row.payload.get("outbox_work_id") == str(terminal_rows[0].id)
            for row in terminal_failure_events
        ), "a terminal failure must record its own durable audit event"

        print(
            f"  retry: {retry_rows[0].attempt_count} attempts, succeeded; "
            f"terminal: {terminal_rows[0].attempt_count} attempts, "
            "terminalized"
        )


# ---------------------------------------------------------------------------
# Conversation-history growth over a long conversation (#83 acceptance
# criterion) -- observed and reported, NOT fixed. The fix for unbounded
# `add_messages` growth (`agent/state.py:11`) is tracked separately
# (Wave 3 #28).
# ---------------------------------------------------------------------------


async def test_conversation_history_growth_over_a_long_conversation() -> None:
    async with boot_live_harness(
        model=_load_test_model(), model_name="load-test-fake"
    ) as harness:
        phone = _phone("hist", 0)
        turn_count = 15
        message_counts: list[int] = []

        for i in range(turn_count):
            before_sent = len(harness.outbound.sent)
            response = await harness.deliver_webhook(
                phone=phone, text=f"message number {i}"
            )
            assert response.status_code == 200, response.text

            def _more_sent_than(baseline: int = before_sent) -> bool:
                return len(harness.outbound.sent) > baseline

            await harness.wait_until(_more_sent_than, timeout=30.0)

            checkpoint = await harness.read_thread(phone)
            assert checkpoint is not None, f"no checkpoint found after turn {i}"
            channel_values = checkpoint.checkpoint.get("channel_values", {})
            messages = channel_values.get("messages", [])
            message_counts.append(len(messages))

        # Never trimmed: strictly non-decreasing, and genuinely larger at the
        # end than at the start -- the exact gap this test DOCUMENTS without
        # fixing (Wave 3 #28 owns the fix).
        assert message_counts == sorted(message_counts), (
            f"history shrank at some point: {message_counts}"
        )
        assert message_counts[-1] > message_counts[0], (
            f"history never grew across {turn_count} turns: {message_counts}"
        )
        print(f"  history growth over {turn_count} sequential turns: {message_counts}")


# ---------------------------------------------------------------------------
# Small real-model run (#83 Phase 3, layer 2): 20 concurrent conversations
# through a REAL provider, reporting tokens/cost/latency.
# ---------------------------------------------------------------------------


#: Generous, not tight: `run_turn` is bounded per attempt by
#: `total_execution_timeout_s` (60s, `loader.PLATFORM_DEFAULT_LIMITS`), and a
#: turn whose model call keeps missing that budget under load retries up to
#: `MAX_OUTBOX_ATTEMPTS` (5) times with exponential backoff before
#: terminalizing -- worst case, comfortably over 300s for ONE conversation.
#: This scenario reports whatever completes inside the window; a real
#: provider's own capacity under concurrency is exactly what it exists to
#: observe, never a platform guarantee to gate on.
_REAL_MODEL_PER_CONVERSATION_TIMEOUT_S = 240.0


@pytest.mark.live
async def test_small_real_model_load_run_reports_tokens_cost_and_latency() -> None:
    """20 concurrent conversations through a real provider (`EVAL_PROVIDER`).

    Runs once (recommended: `EVAL_RUNS=1`) -- this is a load/REPORTING
    scenario, not a gated happy-path one like Phase 1's `_run_gated`. There is
    no pass/fail threshold here, on the model's answers OR on how many
    conversations complete within the window: a real provider's own latency
    and capacity under 20 CONCURRENT requests -- never exercised sequentially
    by any existing live-eval scenario -- is precisely the real-world-under-
    load behavior this scenario exists to observe and report, not something
    the platform controls or this test can assert a floor on. The only hard
    assertions are the same system-level guarantees the deterministic layer
    above already proves without a real provider's variance: nothing that
    completes is ever lost or recorded twice, and at least one conversation
    completes at all (the harness itself works).
    """
    model, model_name = build_eval_model()
    concurrency = 20
    async with boot_live_harness(model=model, model_name=model_name) as harness:
        limiter = harness.app.state.turn_admission_limiter
        phones = [_phone("real", i) for i in range(concurrency)]

        results = await asyncio.gather(
            *(
                _deliver_and_measure(
                    harness,
                    phone=phone,
                    text="Do you have Item Alpha in stock?",
                    timeout=_REAL_MODEL_PER_CONVERSATION_TIMEOUT_S,
                )
                for phone in phones
            ),
            return_exceptions=True,
        )
        latencies = [value for value in results if isinstance(value, float)]
        pending = [
            phones[index]
            for index, value in enumerate(results)
            if isinstance(value, BaseException)
        ]
        completion_rate = len(latencies) / concurrency

        to_counts = Counter(item["to"] for item in harness.outbound.sent)
        for phone in phones:
            if phone in pending:
                assert to_counts[phone] == 0, (
                    f"{phone}: still in flight from this test's own "
                    f"perspective ({_REAL_MODEL_PER_CONVERSATION_TIMEOUT_S:.0f}s) "
                    "but already landed a send -- its reply simply arrived "
                    "after this test stopped waiting, not lost"
                )
            else:
                assert to_counts[phone] == 1, (
                    f"{phone}: expected exactly 1 send, observed {to_counts[phone]}"
                )
        assert latencies, (
            "not one of 20 concurrent real-model conversations completed "
            f"within {_REAL_MODEL_PER_CONVERSATION_TIMEOUT_S:.0f}s -- the "
            "harness/provider path itself may be broken, not just slow"
        )

        metrics_text = await harness.scrape_metrics()
        input_tokens = metric_value(
            metrics_text,
            "agent_tokens_total",
            runtime_id=harness.runtime_id,
            direction="input",
        )
        output_tokens = metric_value(
            metrics_text,
            "agent_tokens_total",
            runtime_id=harness.runtime_id,
            direction="output",
        )
        cost_usd = metric_value(
            metrics_text, "agent_cost_usd_total", runtime_id=harness.runtime_id
        )

        print(
            f"real-model load[{concurrency}] model={model_name} "
            f"limiter_ceiling={limiter.max_concurrent_turns}: "
            f"completed={len(latencies)}/{concurrency} "
            f"({completion_rate:.0%}) still_pending_at_{_REAL_MODEL_PER_CONVERSATION_TIMEOUT_S:.0f}s={pending} "
            f"p50={_percentile(latencies, 50):.2f}s "
            f"p95={_percentile(latencies, 95):.2f}s "
            f"max={max(latencies):.2f}s "
            f"input_tokens={input_tokens} output_tokens={output_tokens} "
            f"cost_usd={cost_usd}"
        )
