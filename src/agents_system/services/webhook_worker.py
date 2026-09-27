"""Deferred processor for durable webhook outbox work, for every channel.

W2b2 wires this worker's ``start``/``stop`` lifecycle into the application
lifespan and replaces the synchronous webhook path: the live route only
persists an inbound message and returns; a turn runs, and a reply is sent,
entirely through this worker's poll loop.

#22 -- the worker is channel-agnostic. Each inbox row names its channel;
the worker hands the row back to that channel's adapter
(``agents_system.channels.port.ChannelAdapter``) to extract the turn and to
send the reply, and runs the turn on the runtime bound to that channel.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

import structlog
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agents_system.channels.port import ChannelAdapter, TextSender
from agents_system.channels.whatsapp import WHATSAPP_CHANNEL, WhatsAppChannel
from agents_system.models.outbox import LEGACY_CHANNEL, InboundMessage, OutboxWork
from agents_system.services.admission import (
    DEFAULT_MAX_CONCURRENT_TURNS,
    TurnAdmissionLimiter,
)
from agents_system.services.outbox import (
    DEFAULT_LEASE_DURATION,
    OutboxLeaseLostError,
    channel_native_key,
    channel_scoped_key,
    claim_available_outbox_work,
    complete_outbox_work,
    persist_outbound_intent,
    record_outbox_failure,
)
from agents_system.services.participants import (
    ConversationRecorder,
    ParticipantDirectory,
)

logger = structlog.get_logger()


class _Runtime(Protocol):
    async def run_turn(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...] | None = None,
        thread_id: str | None = None,
    ) -> list[AnyMessage]: ...


TerminalFailureNotifier = Callable[[OutboxWork, str], Awaitable[None]]
SessionFactory = Callable[[], Any]


@dataclass(frozen=True)
class ChannelBinding:
    """What the worker needs to serve one channel's rows (#22).

    ``checkpointer_enabled`` passes a ``thread_id`` to ``run_turn`` -- the
    conversation, scoped to the channel -- so the turn joins the shared
    checkpointer whenever the process built one.
    """

    adapter: ChannelAdapter
    runtime: _Runtime
    checkpointer_enabled: bool = True


class DeferredWebhookWorker:
    """Process durable claims with explicit at-least-once send semantics.

    A reply is committed before the provider send. If a send outcome is
    ambiguous, a later claim sends that committed reply again without rerunning
    the agent. Meta has no supported idempotency parameter for this request,
    so a duplicate outbound message remains possible by design.

    ``start``/``stop`` run ``process_available`` on a poll loop, the same
    background-task shape ``AuditSink`` uses (construct, ``start()``, push
    ``stop()`` on the lifespan's exit stack). ``stop()`` hard-cancels the loop
    rather than draining it: a claim cancelled mid-flight leaves its lease
    exactly as committed, so a fresh claim recovers it once that lease
    expires -- the same guarantee a mid-turn cancellation already has
    (``process_claimed_work`` lets ``asyncio.CancelledError`` propagate
    instead of recording a failure). No exactly-once execution is implied.

    Channels (#22): ``runtime`` + ``whatsapp_client`` (+
    ``checkpointer_enabled``) bind the built-in WhatsApp channel, exactly as
    before; ``channels`` binds any other registered channel by name. A row
    whose channel has no binding in this process is recorded as a failure
    -- retried, then terminal and visible -- never guessed at.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        worker_id: str,
        directory: ParticipantDirectory | None,
        runtime: _Runtime | None = None,
        whatsapp_client: TextSender | None = None,
        recorder: ConversationRecorder | None = None,
        checkpointer_enabled: bool = False,
        terminal_notifier: TerminalFailureNotifier | None = None,
        poll_interval_s: float = 1.0,
        claim_limit: int = 10,
        admission_limiter: TurnAdmissionLimiter | None = None,
        channels: Mapping[str, ChannelBinding] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        if (runtime is None) != (whatsapp_client is None):
            raise ValueError(
                "runtime and whatsapp_client bind the WhatsApp channel "
                "together: pass both or neither"
            )
        bindings = dict(channels or {})
        if runtime is not None and whatsapp_client is not None:
            if WHATSAPP_CHANNEL in bindings:
                raise ValueError(
                    f"channel {WHATSAPP_CHANNEL!r} is bound twice: by "
                    "runtime/whatsapp_client and by channels"
                )
            bindings[WHATSAPP_CHANNEL] = ChannelBinding(
                adapter=WhatsAppChannel(sender=whatsapp_client),
                runtime=runtime,
                checkpointer_enabled=checkpointer_enabled,
            )
        if not bindings:
            raise ValueError("a webhook worker needs at least one channel binding")
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._directory = directory
        self._channels = bindings
        self._recorder = recorder
        self._terminal_notifier = terminal_notifier
        self._poll_interval_s = poll_interval_s
        self._claim_limit = claim_limit
        # #46 -- shared, process-wide bound (see services/admission.py). A
        # caller that shares one limiter across every turn-running entry
        # point (main.py's lifespan does, for the OpenAI adapter too) gets a
        # true process-wide cap; falling back to a private limiter here just
        # keeps a worker built without one (e.g. existing tests) bounded on
        # its own, never unbounded.
        self._admission_limiter = admission_limiter or TurnAdmissionLimiter(
            DEFAULT_MAX_CONCURRENT_TURNS
        )
        self._started = False
        self._loop_task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        """Whether the poll loop is spawned, started, and still alive (#141).

        Read by ``GET /health`` to report worker liveness alongside the
        outbox backlog counts. Tracks ``start``/``stop`` (``_started``), but
        also requires the loop task to exist and not be ``done()`` (#141
        review follow-up, BLOCKER) -- ``_started`` alone assumed the loop
        can only ever end through ``stop()``, which is not true: a task can
        also die from something ``_run_loop`` does not catch (e.g. a bug
        raising a ``BaseException`` other than ``CancelledError``, or the
        event loop itself being torn down), leaving ``_started`` stuck at
        ``True`` forever while nothing is actually polling the outbox --
        exactly the silent, invisible-to-``/health`` failure #141 exists to
        surface. A dead, un-``stop()``-ped task now reads as not running.
        """
        return (
            self._started and self._loop_task is not None and not self._loop_task.done()
        )

    async def start(self) -> None:
        """Spawn the poll loop. Idempotent (can be called multiple times)."""
        if self._started:
            return
        self._started = True
        self._loop_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Cancel the poll loop and wait for it to finish. Idempotent."""
        if not self._started:
            return
        self._started = False
        task, self._loop_task = self._loop_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        """Claim and process available work on a fixed interval, forever."""
        while True:
            try:
                await self.process_available(limit=self._claim_limit)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "webhook_worker.loop_iteration_failed",
                    worker_id=self._worker_id,
                )
            await asyncio.sleep(self._poll_interval_s)

    async def process_available(self, *, limit: int) -> None:
        """Reserve slots, claim only what was reserved, then process at once.

        #46 SHOULD-FIX 1: admission is reserved with ``try_acquire`` BEFORE
        claiming, not just checked -- a claimed row's lease must never tick
        down while it waits for a slot admission already promised was free.
        At capacity (no slot reserved) nothing is claimed at all -- the
        durable row stays pending in the outbox, no lease is taken and no
        attempt is spent on work that cannot start yet; it is claimed on a
        later poll once a slot frees. A row claimed short of the reserved
        count (or terminalized instead of leased) immediately releases its
        unused reservation rather than holding capacity nothing will use.
        Claimed items are then processed concurrently, each handed its own
        already-reserved slot, so this worker alone never runs more turns at
        once than configured -- and, because the limiter is process-wide,
        neither does the process as a whole across this worker and the
        OpenAI adapter together.
        """
        reserved = 0
        while reserved < limit and self._admission_limiter.try_acquire():
            reserved += 1
        if reserved == 0:
            return

        claimed: list[OutboxWork] = []
        try:
            try:
                async with self._session_factory() as session:
                    claim_outcome = await claim_available_outbox_work(
                        session,
                        worker_id=self._worker_id,
                        limit=reserved,
                        lease_duration=DEFAULT_LEASE_DURATION,
                    )
            except Exception:
                logger.exception(
                    "webhook_worker.claim_failed", worker_id=self._worker_id
                )
                return

            for work in claim_outcome.terminalized:
                await self._notify_terminal_failure(
                    work,
                    work.last_error or "lease expired after maximum delivery attempts",
                )
            claimed = claim_outcome.claimed
        finally:
            # Release every reservation this call did not hand off to a
            # claimed row: a claim failure (early return above) must not
            # leak admission capacity, and claiming fewer rows than
            # reserved (including rows that terminalized instead of being
            # leased) releases the excess immediately.
            for _ in range(reserved - len(claimed)):
                await self._admission_limiter.release()

        if claimed:
            await asyncio.gather(
                *(self._process_claimed_work_admitted(work) for work in claimed)
            )

    async def _process_claimed_work_admitted(self, work: OutboxWork) -> None:
        """Process one claimed item using the slot already reserved for it
        in ``process_available`` -- this never blocks waiting for one."""
        try:
            await self.process_claimed_work(work)
        finally:
            await self._admission_limiter.release()

    async def process_claimed_work(self, work: OutboxWork) -> None:
        """Process one already-committed claim without mutating a stale row."""
        try:
            inbound = await self._load_inbound(work)
        except Exception as exc:
            await self._record_failure(work, exc)
            return

        if inbound is None:
            await self._complete_or_retry(
                work, "inbound message has no serviceable text"
            )
            return

        # A row built in memory (tests) has no server default applied; every
        # persisted row carries its channel (migration 006, NOT NULL).
        channel = inbound.channel or LEGACY_CHANNEL
        binding = self._channels.get(channel)
        if binding is None:
            logger.error(
                "webhook_worker.no_channel_binding",
                outbox_work_id=str(work.id),
                channel=channel,
            )
            await self._record_failure(
                work, RuntimeError(f"no binding for channel {channel!r}")
            )
            return

        try:
            turn = binding.adapter.extract_turn(
                inbound.payload, channel_native_key(channel, inbound.meta_message_id)
            )
        except Exception as exc:
            await self._record_failure(work, exc)
            return
        if turn is None:
            await self._complete_or_retry(
                work, "inbound message has no serviceable text"
            )
            return

        if self._directory is None:
            logger.error(
                "webhook_worker.no_participant_directory",
                outbox_work_id=str(work.id),
                message_id=inbound.meta_message_id,
            )
            await self._record_failure(
                work, RuntimeError("participant directory is unavailable")
            )
            return

        try:
            phone_number = self._directory.normalize_address(turn.sender)
        except ValueError:
            logger.warning(
                "webhook_worker.invalid_address",
                outbox_work_id=str(work.id),
            )
            await self._complete_or_retry(work, "inbound address is not serviceable")
            return
        except Exception as exc:
            await self._record_failure(work, exc)
            return

        try:
            participant = await self._resolve_participant(phone_number)
        except Exception as exc:
            await self._record_failure(work, exc)
            return

        if participant is None or not participant.active:
            await self._complete_or_retry(work, "participant is not active")
            return

        # The conversation this turn belongs to: the normalized address,
        # scoped to its channel so two channels never share one thread.
        conversation = channel_scoped_key(channel, phone_number)
        replay = _persisted_reply(work)
        if replay is None:
            try:
                # #78 Phase 0 (review finding 4, PR #87) -- no `model_id` is
                # passed here on purpose: `AgentRuntime` derives its own
                # default `Settings.model_prices` key from the provider
                # model it was constructed with (`model_display_name`), so
                # this worker's turns are priced correctly without needing a
                # routing id of its own to pass -- previously this call site
                # was exactly why WhatsApp turns were never priced at all.
                result_messages = await binding.runtime.run_turn(
                    messages=[HumanMessage(content=turn.text)],
                    session_id=inbound.meta_message_id,
                    thread_id=conversation if binding.checkpointer_enabled else None,
                )
                assistant_text = _extract_assistant_text(result_messages)
            except Exception as exc:
                await self._record_failure(work, exc)
                return

            await self._record_turn(
                thread_id=conversation,
                participant_id=participant.id,
                user_text=turn.text,
                assistant_text=assistant_text,
            )
            if not assistant_text:
                await self._complete_or_retry(work, "agent produced no reply")
                return

            replay = (
                {"to": phone_number, "body": assistant_text},
                f"{inbound.meta_message_id}:reply",
            )
            try:
                async with self._session_factory() as session:
                    await persist_outbound_intent(
                        session,
                        work=work,
                        worker_id=self._worker_id,
                        body=replay[0],
                        send_key=replay[1],
                    )
            except OutboxLeaseLostError:
                logger.info("webhook_worker.lease_lost", outbox_work_id=str(work.id))
                return
            except Exception as exc:
                await self._record_failure(work, exc)
                return

        body, _send_key = replay
        try:
            await binding.adapter.send_text(to=body["to"], body=body["body"])
        except Exception as exc:
            await self._record_failure(work, exc)
            return

        await self._complete_or_retry(work, "provider send succeeded")

    async def _load_inbound(self, work: OutboxWork) -> InboundMessage | None:
        async with self._session_factory() as session:
            inbound = await session.get(InboundMessage, work.inbound_message_id)
        return cast(InboundMessage | None, inbound)

    async def _resolve_participant(self, phone_number: str) -> Any:
        async with self._session_factory() as session:
            return await self._directory.resolve(session, phone_number)  # type: ignore[union-attr]

    async def _record_turn(
        self,
        *,
        thread_id: str,
        participant_id: Any,
        user_text: str,
        assistant_text: str,
    ) -> None:
        if self._recorder is None:
            return
        try:
            async with self._session_factory() as session:
                await self._recorder.record_turn(
                    session,
                    thread_id=thread_id,
                    participant_id=participant_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
                await session.commit()
        except Exception:
            logger.warning("webhook_worker.recorder_failed", phone_number=thread_id)

    async def _complete_or_retry(self, work: OutboxWork, reason: str) -> None:
        try:
            async with self._session_factory() as session:
                await complete_outbox_work(
                    session,
                    work=work,
                    worker_id=self._worker_id,
                )
        except OutboxLeaseLostError:
            logger.info("webhook_worker.lease_lost", outbox_work_id=str(work.id))
        except Exception as exc:
            await self._record_failure(work, RuntimeError(f"{reason}: {exc}"))

    async def _record_failure(self, work: OutboxWork, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        try:
            async with self._session_factory() as session:
                outcome = await record_outbox_failure(
                    session,
                    work=work,
                    worker_id=self._worker_id,
                    error=error,
                )
        except OutboxLeaseLostError:
            logger.info("webhook_worker.lease_lost", outbox_work_id=str(work.id))
            return
        except Exception:
            logger.exception(
                "webhook_worker.failure_transition_failed",
                outbox_work_id=str(work.id),
            )
            return

        if outcome.terminal and outcome.alert_required:
            await self._notify_terminal_failure(work, error)

    async def _notify_terminal_failure(self, work: OutboxWork, error: str) -> None:
        """Invoke the optional notifier only after its durable signal committed."""
        if self._terminal_notifier is None:
            return
        try:
            await self._terminal_notifier(work, error)
        except Exception:
            # The durable audit event and operator-action signal committed
            # before this deployment-owned callback. There is no durable
            # notification retry record in the W2a schema.
            logger.exception(
                "webhook_worker.terminal_notification_failed",
                outbox_work_id=str(work.id),
            )


def _persisted_reply(work: OutboxWork) -> tuple[dict[str, str], str] | None:
    """Return only a complete committed reply eligible for provider replay."""
    body = work.outbound_body
    send_key = work.outbound_send_key
    if not isinstance(body, dict) or not isinstance(send_key, str) or not send_key:
        return None
    to = body.get("to")
    text = body.get("body")
    if not isinstance(to, str) or not isinstance(text, str):
        return None
    reply: dict[str, str] = {"to": str(to), "body": str(text)}
    return reply, str(send_key)


def _extract_assistant_text(messages: list[AnyMessage]) -> str:
    """Extract final assistant text without importing the webhook route."""
    final_text = ""
    for message in messages:
        if not isinstance(message, AIMessage) or message.tool_calls:
            continue
        content = message.content
        if isinstance(content, list):
            content = " ".join(
                block.get("text", "") for block in content if isinstance(block, dict)
            )
        if content:
            final_text = str(content)
    return final_text
