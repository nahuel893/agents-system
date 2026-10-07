"""Who a turn is for and who started it (ADR-011 parts 1 and 2).

A deployed agent serves many end users through one cached runtime. The entry
point that authenticated the request binds a `TurnSubject` (the end user the
turn is for) and a `TurnActor` (who started it) and passes them to
`run_turn`; the model never supplies either. Everything below `run_turn`
reads only `subject.key` for isolation and `subject.channel` /
`subject.address` for delivery.

This module has no dependencies on the rest of the package so the audit
recorder, the runtime and the entry points can all import it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, get_args

import structlog

ActorKind = Literal["channel", "api", "operator", "scheduler", "library"]
ACTOR_KINDS: tuple[str, ...] = get_args(ActorKind)

PARTICIPANT_KEY_PREFIX = "participant:"


@dataclass(frozen=True)
class TurnSubject:
    """The end user a turn is for.

    ``key`` is the isolation key. ``channel`` and ``address`` say where the
    user was reached and are for delivery and audit payloads only: an address
    can be reassigned to another person, so it never keys data and never goes
    in an audit column.
    """

    key: str
    channel: str | None = None
    address: str | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ValueError("TurnSubject.key must not be empty")

    @classmethod
    def for_participant(
        cls,
        participant_id: Any,
        *,
        channel: str | None = None,
        address: str | None = None,
    ) -> TurnSubject:
        """Subject keyed by a directory participant id (ADR-011 question 1)."""
        return cls(
            key=f"{PARTICIPANT_KEY_PREFIX}{participant_id}",
            channel=channel,
            address=address,
        )


@dataclass(frozen=True)
class TurnActor:
    """Who started the turn: a closed ``kind`` plus an ``id``."""

    kind: ActorKind
    id: str

    def __post_init__(self) -> None:
        if self.kind not in ACTOR_KINDS:
            raise ValueError(
                f"unknown actor kind {self.kind!r}; use one of {ACTOR_KINDS}"
            )

    @property
    def label(self) -> str:
        """``<kind>:<id>``, the form stored in the audit ``actor`` column."""
        return f"{self.kind}:{self.id}"


_subject: ContextVar[TurnSubject | None] = ContextVar("turn_subject", default=None)
_actor: ContextVar[TurnActor | None] = ContextVar("turn_actor", default=None)


def current_subject() -> TurnSubject | None:
    """The subject of the turn running in this context, if any."""
    return _subject.get()


def current_actor() -> TurnActor | None:
    """The actor of the turn running in this context, if any."""
    return _actor.get()


@contextmanager
def turn_scope(subject: TurnSubject | None, actor: TurnActor | None) -> Iterator[None]:
    """Bind subject and actor for one turn, and restore on exit.

    Turn logs pick up the subject key and the actor label through structlog's
    context variables; the address is deliberately not logged.
    """
    subject_token = _subject.set(subject)
    actor_token = _actor.set(actor)
    log_context: dict[str, str] = {}
    if subject is not None:
        log_context["subject"] = subject.key
    if actor is not None:
        log_context["actor"] = actor.label
    try:
        with structlog.contextvars.bound_contextvars(**log_context):
            yield
    finally:
        _subject.reset(subject_token)
        _actor.reset(actor_token)
