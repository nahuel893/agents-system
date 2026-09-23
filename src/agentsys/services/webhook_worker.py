"""Deferred processor for durable Meta webhook outbox work.

W2b2 wires this worker's ``start``/``stop`` lifecycle into the application
lifespan and replaces the synchronous webhook path: the live route only
persists an inbound message and returns; a turn runs, and a reply is sent,
entirely through this worker's poll loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

import structlog
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage

from agentsys.models.outbox import InboundMessage, OutboxWork
from agentsys.services.admission import (
    DEFAULT_MAX_CONCURRENT_TURNS,
    TurnAdmissionLimiter,
)
from agentsys.services.outbox import (
    DEFAULT_LEASE_DURATION,
    OutboxLeaseLostError,
    claim_available_outbox_work,
    complete_outbox_work,
    persist_outbound_intent,
    record_outbox_failure,
)
from agentsys.services.participants import ConversationRecorder, ParticipantDirectory

logger = structlog.get_logger()


class _Runtime(Protocol):
    async def run_turn(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...] | None = None,
        thread_id: str | None = None,
    ) -> list[AnyMessage]: ...


class _WhatsAppClient(Protocol):
    async def send_text(self, to: str, body: str) -> None: ...


TerminalFailureNotifier = Callable[[OutboxWork, str], Awaitable[None]]
SessionFactory = Callable[[], Any]


@dataclass(frozen=True)
class _InboundTurn:
    meta_message_id: str
    phone_number: str
    text: str


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
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        worker_id: str,
        directory: ParticipantDirectory | None,
        runtime: _Runtime,
        whatsapp_client: _WhatsAppClient,
        recorder: ConversationRecorder | None = None,
        checkpointer_enabled: bool = False,
        terminal_notifier: TerminalFailureNotifier | None = None,
        poll_interval_s: float = 1.0,
        claim_limit: int = 10,
        admission_limiter: TurnAdmissionLimiter | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id must not be empty")
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._directory = directory
        self._runtime = runtime
        self._whatsapp_client = whatsapp_client
        self._recorder = recorder
        self._checkpointer_enabled = checkpointer_enabled
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

        turn = _extract_inbound_turn(inbound) if inbound is not None else None
        if turn is None:
            await self._complete_or_retry(
                work, "inbound message has no serviceable text"
            )
            return

        if self._directory is None:
            logger.error(
                "webhook_worker.no_participant_directory",
                outbox_work_id=str(work.id),
                message_id=turn.meta_message_id,
            )
            await self._record_failure(
                work, RuntimeError("participant directory is unavailable")
            )
            return

        try:
            phone_number = self._directory.normalize_address(turn.phone_number)
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

        replay = _persisted_reply(work)
        if replay is None:
            try:
                result_messages = await self._runtime.run_turn(
                    messages=[HumanMessage(content=turn.text)],
                    session_id=turn.meta_message_id,
                    thread_id=phone_number if self._checkpointer_enabled else None,
                )
                assistant_text = _extract_assistant_text(result_messages)
            except Exception as exc:
                await self._record_failure(work, exc)
                return

            await self._record_turn(
                phone_number=phone_number,
                participant_id=participant.id,
                user_text=turn.text,
                assistant_text=assistant_text,
            )
            if not assistant_text:
                await self._complete_or_retry(work, "agent produced no reply")
                return

            replay = (
                {"to": phone_number, "body": assistant_text},
                f"{turn.meta_message_id}:reply",
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
            await self._whatsapp_client.send_text(to=body["to"], body=body["body"])
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
        phone_number: str,
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
                    thread_id=phone_number,
                    participant_id=participant_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
                await session.commit()
        except Exception:
            logger.warning("webhook_worker.recorder_failed", phone_number=phone_number)

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


def _extract_inbound_turn(inbound: InboundMessage) -> _InboundTurn | None:
    """Find this inbox row's exact Meta message across a shared envelope.

    A message whose Meta ``type`` is not ``"text"`` (image, audio, location,
    document, ...) is intentionally never serviceable here: this platform
    relays text to the agent, not type-specific media, and does not attempt
    one today. That is logged explicitly so it reads as a deliberate,
    durably-completed outcome rather than a silent drop; replying to a
    non-text message is a documented product follow-up, not a bug here.
    """
    entries = inbound.payload.get("entry")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                if message_id != inbound.meta_message_id:
                    continue
                phone_number = message.get("from")
                text_value = message.get("text")
                text = text_value.get("body") if isinstance(text_value, dict) else None
                if not isinstance(phone_number, str) or not isinstance(text, str):
                    message_type = message.get("type")
                    if isinstance(message_type, str) and message_type != "text":
                        logger.info(
                            "webhook_worker.non_text_message",
                            meta_message_id=inbound.meta_message_id,
                            message_type=message_type,
                        )
                    return None
                return _InboundTurn(
                    meta_message_id=inbound.meta_message_id,
                    phone_number=phone_number,
                    text=text,
                )
    return None


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
