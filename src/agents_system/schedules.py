"""User-requested schedules and scheduled turns (#175, ADR-011).

An end user asks the agent over a channel: "send me the weekly sales every
Monday at 8". Three subject-scoped tools (`create_schedule`, `list_schedules`,
`delete_schedule`) keep that user's own schedules; the model never names an
owner or a recipient, because the platform hands each tool the scope handle of
the turn's subject (`agents_system.scope`).

A `ScheduledTurnRunner` runs a due schedule as a turn **for its owner**: the
owner is the turn subject (same role, same per-user data scope, memory and
budget) and `scheduler:<schedule id>` is the actor, so audit reads "the
scheduler, on behalf of this user". The reply is delivered only to the owner,
through the proactive path (`agents_system.proactive`, #251), which authorizes
the recipient and the `send:proactive` grant again at send time.

Guardrails, all enforced here and not by the model:

* `write:schedule` (T2) gates the tools, revalidated at call time, and the
  runner checks that the deployment still grants it before every run.
* Limits (`ScheduleLimits`): schedules per user, the shortest interval,
  quiet hours, and the prompt size. The per-user cap is enforced inside the
  store's own critical section.
* A window is **skipped, not caught up**: a run claimed later than
  `missed_grace_s` after it was due, or inside quiet hours, does not run and
  the schedule moves to its next window. The next window is stored at claim
  time, so one window runs at most once across workers and restarts.
* A scheduled turn cannot create schedules.
* Usage counts against the owner's budget (#252) because the owner is the
  turn's subject; a turn the budget refuses delivers nothing.

A schedule ends when it is deleted or when its owner is deleted: the store is
a `SubjectDataStore`, so the operator export and delete (#257) reach it.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from langchain_core.messages import AIMessage, HumanMessage

from agents_system.harness.registry import Tier, ToolSpec
from agents_system.permissions import UnknownPermissionNameError, covers, resolve
from agents_system.proactive import (
    ProactivePermissionError,
    ProactivePolicy,
    ProactiveSender,
    RecipientDirectory,
    RecipientNotAuthorizedError,
)
from agents_system.scope import InMemoryScope, InMemoryScopeProvider
from agents_system.subject import TurnActor, TurnSubject, current_actor
from agents_system.subject_data import SubjectRef

logger = structlog.get_logger(__name__)

WRITE_SCHEDULE = "write:schedule"
SEND_PROACTIVE = "send:proactive"
SCHEDULES_TABLE = "subject_schedules"
SCHEDULES_STORE = "schedules"

Frequency = Literal["daily", "weekly", "interval"]
FREQUENCIES: tuple[str, ...] = ("daily", "weekly", "interval")
_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_CLOCK = re.compile(r"(?P<h>\d{1,2}):(?P<m>\d{2})")
DAY_S = 86_400
WEEK_S = 7 * DAY_S


class ScheduleRejected(ValueError):
    """The schedule cannot be created; the message is safe to show the model."""

    def __init__(self, message: str, kind: str = "invalid_schedule") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ScheduleLimits:
    """The platform's bounds on one user's schedules. Set by the deployment."""

    max_per_user: int = 5
    #: The shortest gap between two runs, in seconds.
    min_interval_s: int = 3_600
    #: ``(start, end)`` in ``quiet_tz``, may wrap midnight; no run starts in it.
    quiet_hours: tuple[time, time] | None = None
    quiet_tz: str = "UTC"
    #: The zone a schedule uses when the user names none.
    default_tz: str = "UTC"
    max_prompt_chars: int = 500
    #: A run claimed later than this after its window is skipped, not run late.
    missed_grace_s: int = 1_800

    def __post_init__(self) -> None:
        for name in ("max_per_user", "min_interval_s", "max_prompt_chars"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer, got {value!r}")
        if (
            isinstance(self.missed_grace_s, bool)
            or not isinstance(self.missed_grace_s, int)
            or self.missed_grace_s < 0
        ):
            raise ValueError(
                f"missed_grace_s must be a non-negative integer, got "
                f"{self.missed_grace_s!r}"
            )
        _zone(self.quiet_tz)
        _zone(self.default_tz)

    def quiet_until(self, when: datetime) -> datetime | None:
        """When quiet hours end, if ``when`` falls inside them."""
        if self.quiet_hours is None:
            return None
        policy = ProactivePolicy(quiet_hours=self.quiet_hours, tz=_zone(self.quiet_tz))
        return policy.quiet_until(when)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ScheduleRejected(
            f"unknown time zone {name!r}; use an IANA name such as 'UTC'",
            "invalid_schedule",
        ) from exc


def limits_from_settings(settings: Any) -> ScheduleLimits:
    """The deployment's limits: the ``SCHEDULE_*`` settings."""
    quiet: tuple[time, time] | None = None
    if settings.schedule_quiet_start and settings.schedule_quiet_end:
        quiet = (
            time.fromisoformat(settings.schedule_quiet_start),
            time.fromisoformat(settings.schedule_quiet_end),
        )
    return ScheduleLimits(
        max_per_user=settings.schedule_max_per_user,
        min_interval_s=settings.schedule_min_interval_s,
        quiet_hours=quiet,
        quiet_tz=settings.schedule_timezone,
        default_tz=settings.schedule_timezone,
        max_prompt_chars=settings.schedule_max_prompt_chars,
        missed_grace_s=settings.schedule_missed_grace_s,
    )


@dataclass(frozen=True)
class Recurrence:
    """When a schedule fires: daily or weekly at a wall-clock time, or every N."""

    frequency: Frequency
    time_of_day: time | None = None
    weekday: int | None = None
    interval_s: int | None = None
    tz: str = "UTC"

    def __post_init__(self) -> None:
        if self.frequency not in FREQUENCIES:
            raise ScheduleRejected(f"frequency must be one of {FREQUENCIES}")
        if self.frequency in ("daily", "weekly") and self.time_of_day is None:
            raise ScheduleRejected("a daily or weekly schedule needs a time (HH:MM)")
        if self.frequency == "weekly" and self.weekday not in range(7):
            raise ScheduleRejected("a weekly schedule needs a weekday")
        if self.frequency == "interval" and (
            self.interval_s is None or self.interval_s < 1
        ):
            raise ScheduleRejected("an interval schedule needs every_hours above 0")
        _zone(self.tz)

    @property
    def min_gap_s(self) -> int:
        """The shortest time between two consecutive runs."""
        if self.frequency == "daily":
            return DAY_S
        if self.frequency == "weekly":
            return WEEK_S
        assert self.interval_s is not None
        return self.interval_s

    def next_after(self, after: datetime) -> datetime:
        """The first run strictly after ``after`` (an aware datetime), in UTC."""
        if self.frequency == "interval":
            assert self.interval_s is not None
            return after.astimezone(UTC) + timedelta(seconds=self.interval_s)
        assert self.time_of_day is not None
        zone = _zone(self.tz)
        first = after.astimezone(zone).date()
        for offset in range(9):
            day = first + timedelta(days=offset)
            if self.frequency == "weekly" and day.weekday() != self.weekday:
                continue
            candidate = datetime.combine(day, self.time_of_day, tzinfo=zone).astimezone(
                UTC
            )
            if candidate > after:
                return candidate
        raise AssertionError("unreachable: a weekday recurs within 8 days")

    def describe(self) -> str:
        if self.frequency == "interval":
            assert self.interval_s is not None
            return f"every {self.interval_s / 3600:g} hours"
        assert self.time_of_day is not None
        at = f"{self.time_of_day.strftime('%H:%M')} {self.tz}"
        if self.frequency == "weekly":
            assert self.weekday is not None
            return f"every {_WEEKDAYS[self.weekday].capitalize()} at {at}"
        return f"every day at {at}"


def parse_recurrence(inputs: Mapping[str, Any], *, default_tz: str) -> Recurrence:
    """A `Recurrence` from the model's tool inputs; raises `ScheduleRejected`."""
    frequency = inputs.get("frequency")
    if frequency not in FREQUENCIES:
        raise ScheduleRejected(f"frequency must be one of {FREQUENCIES}")
    tz = inputs.get("timezone") or default_tz
    if not isinstance(tz, str):
        raise ScheduleRejected("timezone must be an IANA name such as 'UTC'")
    if frequency == "interval":
        hours = inputs.get("every_hours")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours <= 0:
            raise ScheduleRejected("every_hours must be a number above 0")
        return Recurrence("interval", interval_s=round(hours * 3600), tz=tz)
    raw_time = inputs.get("time")
    match = _CLOCK.fullmatch(raw_time.strip()) if isinstance(raw_time, str) else None
    if match is None or int(match["h"]) > 23 or int(match["m"]) > 59:
        raise ScheduleRejected("time must be a 24-hour 'HH:MM' wall-clock time")
    at = time(int(match["h"]), int(match["m"]))
    if frequency == "daily":
        return Recurrence("daily", time_of_day=at, tz=tz)
    name = str(inputs.get("weekday", "")).strip().lower()
    matches = [i for i, d in enumerate(_WEEKDAYS) if name and d.startswith(name[:3])]
    if len(name) < 3 or len(matches) != 1:
        raise ScheduleRejected("weekday must be a day name such as 'monday'")
    return Recurrence("weekly", time_of_day=at, weekday=matches[0], tz=tz)


@dataclass(frozen=True)
class Schedule:
    """One stored schedule. ``prompt`` is what the user asked for, in their words."""

    id: str
    runtime_id: str
    subject_key: str
    prompt: str
    recurrence: Recurrence
    next_run_at: datetime
    created_at: datetime
    last_run_at: datetime | None = None
    last_status: str | None = None


@dataclass(frozen=True)
class ClaimedRun:
    """A due schedule claimed by a worker, already moved to its next window."""

    schedule: Schedule
    due_at: datetime


class ScheduleBackend(Protocol):
    """One subject's schedules, behind a scope handle."""

    async def list(self) -> list[Schedule]: ...

    async def add(self, schedule: Schedule, *, max_per_user: int) -> bool:
        """Store it unless the subject already holds ``max_per_user``."""
        ...

    async def remove(self, schedule_id: str) -> bool: ...


class ScheduleWorkerStore(Protocol):
    """What a runner needs across subjects: claim what is due, record the result."""

    async def claim_due(
        self, now: datetime, advance: Callable[[Schedule, datetime], datetime]
    ) -> ClaimedRun | None: ...

    async def record(
        self,
        runtime_id: str,
        subject_key: str,
        schedule_id: str,
        status: str,
        now: datetime,
    ) -> None: ...


class InMemoryScheduleBackend:
    """Reference backend over `InMemoryScope`: library mode, no Postgres."""

    def __init__(self, scope: InMemoryScope) -> None:
        self._scope = scope

    async def list(self) -> list[Schedule]:
        rows = self._scope.rows(SCHEDULES_TABLE).values()
        return sorted(rows, key=lambda s: (s.created_at, s.id))

    async def add(self, schedule: Schedule, *, max_per_user: int) -> bool:
        async with self._scope.lock:
            if len(self._scope.rows(SCHEDULES_TABLE)) >= max_per_user:
                return False
            self._scope.put(SCHEDULES_TABLE, schedule.id, schedule)
            return True

    async def remove(self, schedule_id: str) -> bool:
        existed = schedule_id in self._scope.rows(SCHEDULES_TABLE)
        self._scope.delete(SCHEDULES_TABLE, schedule_id)
        return existed


def schedule_backend_for(scope: Any) -> ScheduleBackend:
    """The schedule backend behind a scope handle: the handle decides the storage."""
    if isinstance(scope, InMemoryScope):
        return InMemoryScheduleBackend(scope)
    from agents_system.services.schedules_postgres import PostgresScheduleBackend
    from agents_system.services.scope_postgres import PostgresScope

    if isinstance(scope, PostgresScope):
        return PostgresScheduleBackend(scope)
    raise TypeError(f"no schedule backend for a {type(scope).__name__} scope")


def export_row(schedule: Schedule) -> dict[str, Any]:
    """A schedule as the operator export shows it."""
    return {
        "id": schedule.id,
        "prompt": schedule.prompt,
        "when": schedule.recurrence.describe(),
        "next_run_at": schedule.next_run_at.isoformat(),
        "created_at": schedule.created_at.isoformat(),
        "last_run_at": schedule.last_run_at.isoformat()
        if schedule.last_run_at
        else None,
        "last_status": schedule.last_status,
    }


class InMemoryScheduleStore:
    """The in-process store: the worker side plus export and delete (#257)."""

    name = SCHEDULES_STORE
    tables = frozenset({SCHEDULES_TABLE})

    def __init__(self, provider: InMemoryScopeProvider) -> None:
        self._provider = provider

    async def claim_due(
        self, now: datetime, advance: Callable[[Schedule, datetime], datetime]
    ) -> ClaimedRun | None:
        due = [
            (s, rt, sk)
            for rt, sk, _key, s in self._provider.all_rows(SCHEDULES_TABLE)
            if s.next_run_at <= now
        ]
        if not due:
            return None
        schedule, runtime_id, subject_key = min(
            due, key=lambda t: (t[0].next_run_at, t[0].id)
        )
        moved = replace(schedule, next_run_at=advance(schedule, now))
        self._provider.scope(runtime_id, subject_key).put(
            SCHEDULES_TABLE, schedule.id, moved
        )
        return ClaimedRun(moved, schedule.next_run_at)

    async def record(
        self,
        runtime_id: str,
        subject_key: str,
        schedule_id: str,
        status: str,
        now: datetime,
    ) -> None:
        scope = self._provider.scope(runtime_id, subject_key)
        current = scope.get(SCHEDULES_TABLE, schedule_id)
        if current is not None:
            scope.put(
                SCHEDULES_TABLE,
                schedule_id,
                replace(current, last_run_at=now, last_status=status),
            )

    async def export(self, subject: SubjectRef) -> list[dict[str, Any]]:
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        return [export_row(s) for s in await InMemoryScheduleBackend(scope).list()]

    async def delete(self, subject: SubjectRef) -> int:
        scope = self._provider.scope(subject.runtime_id, subject.subject_key)
        backend = InMemoryScheduleBackend(scope)
        return sum([await backend.remove(s.id) for s in await backend.list()])


# --- the tools -----------------------------------------------------------------

_CREATE_DESCRIPTION = (
    "Schedule a recurring request for the person you are talking to: the "
    "platform will run it for them on that schedule and send them the answer, "
    "and only them. 'prompt' is the request exactly as it should be asked "
    "later, complete on its own because the conversation will be gone (for "
    "example 'Send me the weekly sales summary'). Choose frequency 'daily' or "
    "'weekly' with a 24-hour 'time' (and a 'weekday' for weekly), or "
    "'interval' with 'every_hours'. Use the person's own time zone when they "
    "gave one. You cannot choose who receives it."
)
_LIST_DESCRIPTION = (
    "List the recurring requests scheduled for the person you are talking to, "
    "with their ids and next run."
)
_DELETE_DESCRIPTION = (
    "Delete one of this person's scheduled requests by the id list_schedules "
    "returned. Use it when they ask to stop or cancel one."
)


def build_schedule_tool_specs(
    limits: ScheduleLimits | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
) -> tuple[ToolSpec, ToolSpec, ToolSpec]:
    """The three schedule tools, unregistered.

    Each is ``subject_scoped``: the interceptor hands its connector the scope
    handle of the turn's end user, so the tools hold and accept no user id.
    The deployment must grant ``write:schedule`` (T2, revalidated at call time,
    needs a ``DEPLOY_GRANTS`` entry).
    """
    bounds = limits or ScheduleLimits()
    now = clock or (lambda: datetime.now(UTC))

    def refuse(kind: str, message: str) -> dict[str, Any]:
        return {"error": message, "error_kind": kind}

    async def create(
        inputs: dict[str, Any], *, session: Any = None, scope: Any
    ) -> dict[str, Any]:
        actor = current_actor()
        if actor is not None and actor.kind == "scheduler":
            return refuse(
                "scheduled_turn", "a scheduled run cannot create new schedules"
            )
        prompt = inputs.get("prompt")
        if (
            not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt.strip()) > bounds.max_prompt_chars
        ):
            return refuse(
                "invalid_prompt",
                f"prompt must be 1 to {bounds.max_prompt_chars} characters",
            )
        try:
            recurrence = parse_recurrence(inputs, default_tz=bounds.default_tz)
        except ScheduleRejected as exc:
            return refuse(exc.kind, str(exc))
        if recurrence.min_gap_s < bounds.min_interval_s:
            return refuse(
                "interval_too_short",
                f"runs must be at least {bounds.min_interval_s // 60} minutes apart",
            )
        created_at = now()
        next_run = recurrence.next_after(created_at)
        if recurrence.frequency != "interval" and bounds.quiet_until(next_run):
            return refuse(
                "quiet_hours",
                "that time falls inside quiet hours, when nothing is sent; "
                "choose another time",
            )
        schedule = Schedule(
            id=uuid.uuid4().hex[:12],
            runtime_id=scope.runtime_id,
            subject_key=scope.subject_key,
            prompt=prompt.strip(),
            recurrence=recurrence,
            next_run_at=next_run,
            created_at=created_at,
        )
        if not await schedule_backend_for(scope).add(
            schedule, max_per_user=bounds.max_per_user
        ):
            return refuse(
                "limit_reached",
                f"at most {bounds.max_per_user} schedules per person; delete one first",
            )
        return {
            "created": True,
            "id": schedule.id,
            "when": recurrence.describe(),
            "next_run_at": next_run.isoformat(),
        }

    async def list_schedules(
        inputs: dict[str, Any], *, session: Any = None, scope: Any
    ) -> dict[str, Any]:
        return {
            "schedules": [
                {
                    "id": s.id,
                    "prompt": s.prompt,
                    "when": s.recurrence.describe(),
                    "next_run_at": s.next_run_at.isoformat(),
                }
                for s in await schedule_backend_for(scope).list()
            ]
        }

    async def delete(
        inputs: dict[str, Any], *, session: Any = None, scope: Any
    ) -> dict[str, Any]:
        schedule_id = inputs.get("id")
        if not isinstance(schedule_id, str):
            return {"deleted": False}
        return {"deleted": await schedule_backend_for(scope).remove(schedule_id)}

    def spec(**kwargs: Any) -> ToolSpec:
        return ToolSpec(
            subject_scoped=True,
            required_permissions=(WRITE_SCHEDULE,),
            tier=Tier.T2,
            **kwargs,
        )

    return (
        spec(
            name="create_schedule",
            description=_CREATE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The request to run, complete on its own",
                    },
                    "frequency": {"type": "string", "enum": list(FREQUENCIES)},
                    "time": {
                        "type": "string",
                        "description": (
                            "24-hour HH:MM, zero-padded like 08:00, for daily "
                            "and weekly"
                        ),
                    },
                    "weekday": {
                        "type": "string",
                        "enum": list(_WEEKDAYS),
                        "description": "For weekly",
                    },
                    "every_hours": {
                        "type": "number",
                        "description": "Hours between runs, for interval",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "IANA zone such as America/Bogota; optional",
                    },
                },
                "required": ["prompt", "frequency"],
            },
            connector=create,
        ),
        spec(
            name="list_schedules",
            description=_LIST_DESCRIPTION,
            input_schema={"type": "object", "properties": {}},
            connector=list_schedules,
        ),
        spec(
            name="delete_schedule",
            description=_DELETE_DESCRIPTION,
            input_schema={
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "A schedule id from list_schedules",
                    }
                },
                "required": ["id"],
            },
            connector=delete,
        ),
    )


# --- the runner ------------------------------------------------------------------

RunStatus = Literal["idle", "delivered", "skipped", "failed"]


@dataclass(frozen=True)
class RunOutcome:
    """What one `run_once` did. ``idle`` means nothing was due."""

    status: RunStatus
    reason: str | None = None
    schedule_id: str | None = None


class _Turns(Protocol):
    async def run_turn_with_usage(
        self,
        messages: Any,
        session_id: str,
        permissions: Any = None,
        thread_id: str | None = None,
        model_id: str | None = None,
        *,
        subject: TurnSubject | None = None,
        actor: TurnActor | None = None,
        untrusted_data: Any = (),
    ) -> Any: ...


def _classes(names: Collection[str]) -> set[type]:
    found: set[type] = set()
    for name in names:
        try:
            found.add(resolve(name))
        except UnknownPermissionNameError:
            continue
    return found


def _final_text(messages: Sequence[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            content = message.content
            if isinstance(content, str):
                return content.strip()
            return " ".join(
                part.get("text", "") if isinstance(part, dict) else str(part)
                for part in content
            ).strip()
    return ""


class ScheduledTurnRunner:
    """Runs due schedules as turns for their owners and delivers the reply.

    Call `run_once` in a loop in each worker process (like the proactive
    dispatcher). It claims one due schedule, which also moves the schedule to
    its next window, so a window runs at most once across workers.
    """

    def __init__(
        self,
        store: ScheduleWorkerStore,
        runtimes: Mapping[str, _Turns],
        sender: ProactiveSender,
        directory: RecipientDirectory,
        *,
        deploy_grants: Mapping[str, Collection[str]],
        limits: ScheduleLimits | None = None,
    ) -> None:
        self._store = store
        self._runtimes = dict(runtimes)
        self._sender = sender
        self._directory = directory
        self._ceiling = {rt: _classes(names) for rt, names in deploy_grants.items()}
        self._limits = limits or ScheduleLimits()

    def _granted(self, runtime_id: str, permission: str) -> bool:
        needed = resolve(permission)
        return any(covers(g, needed) for g in self._ceiling.get(runtime_id, set()))

    async def run_once(self, *, now: datetime | None = None) -> RunOutcome:
        moment = now or datetime.now(UTC)
        claimed = await self._store.claim_due(
            moment, lambda schedule, at: schedule.recurrence.next_after(at)
        )
        if claimed is None:
            return RunOutcome("idle")
        schedule = claimed.schedule
        outcome = await self._execute(claimed, moment)
        status = (
            outcome.status
            if outcome.reason is None
            else f"{outcome.status}:{outcome.reason}"
        )
        try:
            await self._store.record(
                schedule.runtime_id, schedule.subject_key, schedule.id, status, moment
            )
        except Exception:
            logger.warning(
                "schedule.record_failed", schedule=schedule.id, exc_info=True
            )
        logger.info(
            "schedule.run",
            schedule=schedule.id,
            subject=schedule.subject_key,
            status=outcome.status,
            reason=outcome.reason,
        )
        return outcome

    async def _execute(self, claimed: ClaimedRun, now: datetime) -> RunOutcome:
        schedule = claimed.schedule
        sid = schedule.id

        def out(status: RunStatus, reason: str | None = None) -> RunOutcome:
            return RunOutcome(status, reason, sid)

        runtime = self._runtimes.get(schedule.runtime_id)
        if runtime is None:
            return out("failed", "runtime_unavailable")
        if not self._granted(schedule.runtime_id, WRITE_SCHEDULE):
            return out("skipped", "not_permitted")
        if (now - claimed.due_at).total_seconds() > self._limits.missed_grace_s:
            return out("skipped", "missed_window")
        if self._limits.quiet_until(now) is not None:
            return out("skipped", "quiet_hours")
        subject = TurnSubject(key=schedule.subject_key)
        recipient = await self._directory.authorize(schedule.runtime_id, subject)
        if recipient is None:
            return out("failed", "recipient_not_authorized")
        actor = TurnActor(kind="scheduler", id=sid)
        try:
            result = await runtime.run_turn_with_usage(
                [HumanMessage(content=schedule.prompt)],
                f"schedule:{sid}",
                subject=subject,
                actor=actor,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("schedule.turn_failed", schedule=sid, exc_info=True)
            return out("failed", "turn_failed")
        if getattr(result, "budget_refused", False):
            return out("skipped", "budget_refused")
        if getattr(result, "timed_out", False):
            return out("failed", "turn_timed_out")
        text = _final_text(result.messages)
        if not text:
            return out("skipped", "empty_reply")
        try:
            await self._sender.submit(
                schedule.runtime_id,
                recipient,
                text,
                actor=actor,
                permissions=(SEND_PROACTIVE,),
            )
        except RecipientNotAuthorizedError:
            return out("failed", "recipient_not_authorized")
        except ProactivePermissionError:
            return out("failed", "delivery_not_permitted")
        return out("delivered")
