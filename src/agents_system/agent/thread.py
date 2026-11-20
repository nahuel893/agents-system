"""Conversation threads: the checkpointer-backed history of a thread, the bound on its calls and the progress of a turn."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Sequence
from contextlib import aclosing
from typing import Any

import structlog
from langchain_core.messages import (
    AnyMessage,
    BaseMessage,
)
from langchain_core.runnables import RunnableConfig

from agents_system.agent.state import AgentState
from agents_system.observability.metrics import (
    Metrics,
    record_checkpointer_degraded,
)

logger = structlog.get_logger()


@dataclasses.dataclass
class _TurnProgress:
    """The latest graph state a turn reached, kept outside the invocation so
    a timeout (which cancels it and returns nothing) can still read back
    the tool calls, model calls and usage accumulated so far (issue #172)."""

    state: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Conversation threads and the checkpointer failure policy (issue #24)
# ---------------------------------------------------------------------------

#: How long a thread read or save may take when the caller sets no bound:
#: the same default as the served app's ``CHECKPOINTER_TIMEOUT_S``.
DEFAULT_CHECKPOINTER_TIMEOUT_S = 5.0


#: Checkpointer calls abandoned at their timeout and still being cancelled.
#: Held so the event loop keeps a strong reference until they finish.
_ABANDONED_CALLS: set[asyncio.Future[Any]] = set()


def _forget_abandoned(call: asyncio.Future[Any]) -> None:
    _ABANDONED_CALLS.discard(call)
    if not call.cancelled():
        # Retrieved, so asyncio does not report it as never retrieved; the
        # failure was already handled as a timeout.
        call.exception()


async def _within[T](timeout_s: float, call: Awaitable[T]) -> T:
    """Await *call*, giving up after *timeout_s* seconds with ``TimeoutError``.

    A database that stops answering without refusing anything -- a frozen
    server, a network partition -- raises nothing, and a server-side
    ``statement_timeout`` cannot end the wait either. This bound is on the
    client, so it holds for any saver.

    An abandoned call is cancelled but not awaited: a driver can take a
    while to cancel a request (psycopg waits up to ten seconds for the
    server), and the turn does not wait for that. The cancellation finishes
    in the background, which releases the call's connection.
    """
    task = asyncio.ensure_future(call)
    bound = asyncio.timeout(timeout_s)
    try:
        async with bound:
            return await asyncio.shield(task)
    except TimeoutError:
        if not bound.expired():
            raise  # the call's own error, not this bound
        raise TimeoutError(
            f"the checkpointer did not answer within {timeout_s}s"
        ) from None
    finally:
        if not task.done():
            task.cancel()
            _ABANDONED_CALLS.add(task)
            task.add_done_callback(_forget_abandoned)


@dataclasses.dataclass
class _Thread:
    """One conversation thread in the runtime's checkpointer (issue #24).

    The graph always runs WITHOUT a checkpointer. The thread's history is
    read once before the turn and the turn's new messages are saved once
    after it, so the checkpointer is touched at exactly two points and a
    thread only ever holds whole turns: a process that dies in the middle of
    a turn leaves the thread where the previous turn ended.

    The failure policy keys on which of those two points failed, never on
    the exception class a particular client library raises, so it is the
    same for Postgres, Redis, SQLite or any other LangGraph saver:

    - The read fails (the backend is down, the pool is exhausted, or this
      thread's stored checkpoint cannot be loaded): the turn runs on its own
      messages only, and it is NOT saved -- a thread that could not be read
      is never overwritten by one that lacks its earlier turns.
    - The save fails: the finished answer is returned as it is. Nothing runs
      again, so no model call is repeated and no tool call is replayed; the
      thread stays at its previous turn.

    A backend that never answers -- a frozen or partitioned database server
    raises nothing -- is a failure too: each read and each save is abandoned
    after ``timeout_s`` (``_within``) and handled as the failure of that
    point.

    Both are logged as ``runtime.checkpointer_degraded`` with the
    ``operation`` that failed. A broken thread therefore keeps answering
    without its memory and never affects another thread. ``CancelledError``
    is not an ``Exception`` and is never caught here.
    """

    compiled: Any
    thread_id: str
    timeout_s: float
    metrics: Metrics
    #: What the read returned; ``None`` until a read succeeds.
    history: list[AnyMessage] | None = None

    @property
    def config(self) -> RunnableConfig:
        return {"configurable": {"thread_id": self.thread_id}}

    async def load(self) -> list[AnyMessage]:
        """The thread's history, or ``[]`` when it cannot be read."""
        try:
            snapshot = await _within(
                self.timeout_s, self.compiled.aget_state(self.config)
            )
            history = snapshot.values.get("messages", [])
            # A damaged checkpoint can decode into something that is not a
            # message list at all; that is an unreadable thread too.
            if not isinstance(history, list) or not all(
                isinstance(m, BaseMessage) for m in history
            ):
                raise TypeError("the stored thread is not a list of messages")
        except Exception as exc:
            self._degraded("read", exc)
            return []
        self.history = list(history)
        return self.history

    async def save(self, new_messages: Sequence[AnyMessage]) -> None:
        """Append this turn's messages to the thread, as one checkpoint.

        Saved as if ``call_model`` had written them: the turn always ends
        with a reply that routes to ``END``, so the saved checkpoint has no
        pending step. A save abandoned at its timeout can still complete if
        the database was already writing it; the thread then holds this
        turn, whole, exactly as if the save had answered in time.
        """
        if self.history is None or not new_messages:
            return
        try:
            await _within(
                self.timeout_s,
                self.compiled.aupdate_state(
                    self.config, {"messages": list(new_messages)}, as_node="call_model"
                ),
            )
        except Exception as exc:
            self._degraded("write", exc)

    def _degraded(self, operation: str, exc: Exception) -> None:
        logger.warning(
            "runtime.checkpointer_degraded",
            operation=operation,
            thread_id=self.thread_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        # Counted as well: a checkpointer that fails for every turn (a role
        # that cannot save, a database that stopped answering) would
        # otherwise show only as turns that all report "ok".
        record_checkpointer_degraded(self.metrics, operation=operation)


async def _run_tracking_progress(
    compiled: Any,
    initial_state: AgentState,
    config: RunnableConfig,
    tracker: _TurnProgress,
) -> dict[str, Any]:
    """Run *compiled* to completion, recording the state after every step.

    ``astream(stream_mode="values")`` yields the full state after each
    super-step; its last value is exactly what ``ainvoke`` would return. The
    difference is that a cancellation (the turn budget) now leaves the last
    completed step in *tracker* instead of losing everything (issue #172).
    """
    final: dict[str, Any] | None = None
    async with aclosing(
        compiled.astream(initial_state, config=config, stream_mode="values")
    ) as stream:
        async for state in stream:
            final = dict(state)
            tracker.state = final
    if final is None:  # pragma: no cover -- a graph always yields its input
        raise RuntimeError("graph produced no state")
    return final
