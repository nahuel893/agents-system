"""User-requested schedules and scheduled turns (#175, ADR-011).

Recurrence, limits, the three subject-scoped tools, the in-process store and
the runner that executes a due schedule as a turn for its owner.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agents_system.proactive import (
    InMemoryProactiveQueue,
    ProactiveSender,
    StaticRecipientDirectory,
)
from agents_system.schedules import (
    InMemoryScheduleStore,
    Recurrence,
    Schedule,
    ScheduledTurnRunner,
    ScheduleLimits,
    ScheduleRejected,
    build_schedule_tool_specs,
    parse_recurrence,
    schedule_backend_for,
)
from agents_system.scope import InMemoryScopeProvider
from agents_system.subject import TurnSubject, current_actor, turn_scope

RT = "rt"
ALICE = TurnSubject.for_participant(1, channel="whatsapp", address="+100")
BOB = TurnSubject.for_participant(2, channel="whatsapp", address="+200")
UTC_MONDAY_7 = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)  # a Monday
GRANTS = {RT: {"send:proactive", "write:schedule"}}


# --- recurrence ---------------------------------------------------------------


def test_weekly_next_run_is_the_next_matching_weekday_at_the_time() -> None:
    rec = Recurrence("weekly", time_of_day=time(8, 0), weekday=0, tz="UTC")
    assert rec.next_after(UTC_MONDAY_7) == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    # Strictly after: at 08:00 sharp the next one is a week later.
    assert rec.next_after(datetime(2026, 10, 5, 8, 0, tzinfo=UTC)) == datetime(
        2026, 10, 12, 8, 0, tzinfo=UTC
    )


def test_daily_next_run_honors_the_time_zone() -> None:
    rec = Recurrence(
        "daily", time_of_day=time(8, 0), tz="America/Argentina/Buenos_Aires"
    )
    # 08:00 in UTC-3 is 11:00 UTC.
    assert rec.next_after(UTC_MONDAY_7) == datetime(2026, 10, 5, 11, 0, tzinfo=UTC)


def test_interval_next_run_adds_the_interval() -> None:
    rec = Recurrence("interval", interval_s=7200, tz="UTC")
    assert rec.next_after(UTC_MONDAY_7) == UTC_MONDAY_7 + timedelta(hours=2)


@pytest.mark.parametrize(
    "inputs",
    [
        {"frequency": "yearly"},
        {"frequency": "daily"},
        {"frequency": "daily", "time": "25:00"},
        {"frequency": "daily", "time": "08:00", "timezone": "Mars/Base"},
        {"frequency": "weekly", "time": "08:00"},
        {"frequency": "weekly", "time": "08:00", "weekday": "someday"},
        {"frequency": "interval", "every_hours": 0},
        {"frequency": "interval", "every_hours": "often"},
    ],
)
def test_a_malformed_recurrence_is_rejected(inputs: dict[str, Any]) -> None:
    with pytest.raises(ScheduleRejected):
        parse_recurrence(inputs, default_tz="UTC")


def test_parse_recurrence_accepts_an_unpadded_hour() -> None:
    rec = parse_recurrence(
        {"frequency": "daily", "time": "8:05", "timezone": "UTC"}, default_tz="UTC"
    )
    assert rec.time_of_day == time(8, 5)


def test_parse_recurrence_accepts_weekday_names_and_a_default_zone() -> None:
    rec = parse_recurrence(
        {"frequency": "weekly", "time": "08:00", "weekday": "Monday"}, default_tz="UTC"
    )
    assert rec == Recurrence("weekly", time_of_day=time(8, 0), weekday=0, tz="UTC")


# --- tools --------------------------------------------------------------------


def _tools(limits: ScheduleLimits | None = None) -> dict[str, Any]:
    specs = build_schedule_tool_specs(limits)
    return {s.name: s for s in specs}


async def _call(
    tools: dict[str, Any], name: str, inputs: dict[str, Any], scope: Any
) -> dict[str, Any]:
    result: dict[str, Any] = await tools[name].connector(inputs, scope=scope)
    return result


_WEEKLY = {
    "prompt": "Send me the weekly sales summary",
    "frequency": "weekly",
    "time": "08:00",
    "weekday": "monday",
    "timezone": "UTC",
}


def test_the_tools_are_subject_scoped_t2_and_need_write_schedule() -> None:
    tools = _tools()
    assert set(tools) == {"create_schedule", "list_schedules", "delete_schedule"}
    for spec in tools.values():
        assert spec.subject_scoped is True
        assert spec.required_permissions == ("write:schedule",)
        assert spec.tier.name == "T2"
        # The model never names the recipient or the owner.
        props = spec.input_schema.get("properties", {})
        assert not {"user", "recipient", "subject", "address", "to"} & set(props)


async def test_create_list_delete_round_trip_for_one_subject() -> None:
    provider = InMemoryScopeProvider()
    tools = _tools()
    scope = provider.scope(RT, ALICE.key)
    created = await _call(tools, "create_schedule", _WEEKLY, scope)
    assert created["created"] is True
    listed = await _call(tools, "list_schedules", {}, scope)
    assert [s["id"] for s in listed["schedules"]] == [created["id"]]
    assert listed["schedules"][0]["prompt"] == _WEEKLY["prompt"]
    deleted = await _call(tools, "delete_schedule", {"id": created["id"]}, scope)
    assert deleted == {"deleted": True}
    assert (await _call(tools, "list_schedules", {}, scope))["schedules"] == []


async def test_one_user_cannot_see_or_delete_anothers_schedule() -> None:
    provider = InMemoryScopeProvider()
    tools = _tools()
    created = await _call(
        tools, "create_schedule", _WEEKLY, provider.scope(RT, ALICE.key)
    )
    bob = provider.scope(RT, BOB.key)
    assert (await _call(tools, "list_schedules", {}, bob))["schedules"] == []
    assert await _call(tools, "delete_schedule", {"id": created["id"]}, bob) == {
        "deleted": False
    }
    alice_list = await _call(tools, "list_schedules", {}, provider.scope(RT, ALICE.key))
    assert len(alice_list["schedules"]) == 1


async def test_the_per_user_cap_is_enforced_atomically() -> None:
    provider = InMemoryScopeProvider()
    tools = _tools(ScheduleLimits(max_per_user=2))
    scope = provider.scope(RT, ALICE.key)
    results = await asyncio.gather(
        *[_call(tools, "create_schedule", _WEEKLY, scope) for _ in range(5)]
    )
    assert sum(1 for r in results if r.get("created")) == 2
    assert {r["error_kind"] for r in results if not r.get("created")} == {
        "limit_reached"
    }


async def test_an_interval_below_the_minimum_is_refused() -> None:
    scope = InMemoryScopeProvider().scope(RT, ALICE.key)
    tools = _tools(ScheduleLimits(min_interval_s=3600))
    out = await _call(
        tools,
        "create_schedule",
        {"prompt": "ping", "frequency": "interval", "every_hours": 0.25},
        scope,
    )
    assert out["error_kind"] == "interval_too_short"


async def test_a_time_inside_quiet_hours_is_refused() -> None:
    scope = InMemoryScopeProvider().scope(RT, ALICE.key)
    limits = ScheduleLimits(quiet_hours=(time(22, 0), time(7, 0)))
    out = await _call(
        _tools(limits),
        "create_schedule",
        {"prompt": "x", "frequency": "daily", "time": "23:30", "timezone": "UTC"},
        scope,
    )
    assert out["error_kind"] == "quiet_hours"


async def test_an_empty_or_oversized_prompt_is_refused() -> None:
    scope = InMemoryScopeProvider().scope(RT, ALICE.key)
    tools = _tools(ScheduleLimits(max_prompt_chars=20))
    for prompt in ("   ", "x" * 21):
        out = await _call(
            tools, "create_schedule", {**_WEEKLY, "prompt": prompt}, scope
        )
        assert out["error_kind"] == "invalid_prompt"


async def test_a_scheduled_turn_cannot_create_schedules() -> None:
    from agents_system.subject import TurnActor

    scope = InMemoryScopeProvider().scope(RT, ALICE.key)
    with turn_scope(ALICE, TurnActor(kind="scheduler", id="abc")):
        out = await _call(_tools(), "create_schedule", _WEEKLY, scope)
    assert out["error_kind"] == "scheduled_turn"
    assert (await _call(_tools(), "list_schedules", {}, scope))["schedules"] == []


# --- runner -------------------------------------------------------------------


class FakeTurnResult:
    def __init__(self, text: str, *, refused: bool = False) -> None:
        self.messages = [HumanMessage(content="q"), AIMessage(content=text)]
        self.budget_refused = refused
        self.timed_out = False


class FakeRuntime:
    def __init__(
        self, text: str = "Weekly sales: 42", *, refused: bool = False
    ) -> None:
        self.text, self.refused = text, refused
        self.calls: list[dict[str, Any]] = []

    async def run_turn_with_usage(
        self, messages: Any, session_id: str, **kw: Any
    ) -> Any:
        # Like the real runtime: the subject and actor are visible in-turn.
        with turn_scope(kw.get("subject"), kw.get("actor")):
            self.calls.append(
                {
                    "messages": messages,
                    "session_id": session_id,
                    "kw": kw,
                    "actor": current_actor(),
                }
            )
        return FakeTurnResult(self.text, refused=self.refused)


class Env:
    def __init__(
        self,
        *,
        runtime: FakeRuntime | None = None,
        limits: ScheduleLimits | None = None,
        recipients: bool = True,
        grants: Any = None,
    ) -> None:
        self.provider = InMemoryScopeProvider()
        self.store = InMemoryScheduleStore(self.provider)
        self.queue = InMemoryProactiveQueue()
        self.directory = StaticRecipientDirectory(
            {(RT, ALICE.key): ALICE} if recipients else {}
        )
        self.grants = grants if grants is not None else GRANTS
        self.sender = ProactiveSender(
            self.queue, self.directory, deploy_grants=self.grants
        )
        self.runtime = runtime or FakeRuntime()
        self.limits = limits or ScheduleLimits()
        self.runner = ScheduledTurnRunner(
            self.store,
            {RT: self.runtime},
            self.sender,
            self.directory,
            deploy_grants=self.grants,
            limits=self.limits,
        )

    async def add(
        self, subject: TurnSubject = ALICE, due: datetime | None = None
    ) -> Schedule:
        sched = Schedule(
            id="s1",
            runtime_id=RT,
            subject_key=subject.key,
            prompt="Send the weekly sales",
            recurrence=Recurrence(
                "weekly", time_of_day=time(8, 0), weekday=0, tz="UTC"
            ),
            next_run_at=due or UTC_MONDAY_7,
            created_at=UTC_MONDAY_7 - timedelta(days=3),
        )
        assert await schedule_backend_for(self.provider.scope(RT, subject.key)).add(
            sched, max_per_user=9
        )
        return sched

    async def queued(self) -> list[Any]:
        msg = await self.queue.claim(
            "w", now=datetime.now(UTC) + timedelta(days=1), lease_s=60
        )
        return [msg] if msg else []


async def test_a_due_schedule_runs_as_a_turn_for_its_owner_and_delivers_text() -> None:
    env = Env()
    await env.add()
    out = await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    assert out.status == "delivered"
    call = env.runtime.calls[0]
    assert call["kw"]["subject"].key == ALICE.key
    actor = call["kw"]["actor"]
    assert (actor.kind, actor.id) == ("scheduler", "s1")
    assert call["actor"].label == "scheduler:s1"
    assert call["messages"][0].content == "Send the weekly sales"
    # No explicit permissions: the runtime's own deploy ceiling applies.
    assert call["kw"].get("permissions") is None
    (queued,) = await env.queued()
    assert (queued.subject_key, queued.text, queued.actor) == (
        ALICE.key,
        "Weekly sales: 42",
        "scheduler:s1",
    )


async def test_the_next_run_advances_and_the_same_window_never_runs_twice() -> None:
    env = Env()
    await env.add()
    now = UTC_MONDAY_7 + timedelta(minutes=1)
    assert (await env.runner.run_once(now=now)).status == "delivered"
    assert (await env.runner.run_once(now=now)).status == "idle"
    listed = await schedule_backend_for(env.provider.scope(RT, ALICE.key)).list()
    assert (
        listed[0].next_run_at == datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
        or listed[0].next_run_at > now
    )
    assert listed[0].last_status == "delivered"


async def test_a_missed_window_is_skipped_and_runs_at_the_next_one() -> None:
    env = Env(limits=ScheduleLimits(missed_grace_s=600))
    await env.add()
    late = UTC_MONDAY_7 + timedelta(hours=5)
    out = await env.runner.run_once(now=late)
    assert (out.status, out.reason) == ("skipped", "missed_window")
    assert env.runtime.calls == []
    listed = await schedule_backend_for(env.provider.scope(RT, ALICE.key)).list()
    assert listed[0].next_run_at > late
    assert listed[0].last_status == "skipped:missed_window"


async def test_a_run_inside_quiet_hours_is_skipped() -> None:
    env = Env(limits=ScheduleLimits(quiet_hours=(time(6, 0), time(9, 0))))
    await env.add()
    out = await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    assert (out.status, out.reason) == ("skipped", "quiet_hours")
    assert env.runtime.calls == []


async def test_a_budget_refused_turn_delivers_nothing() -> None:
    env = Env(runtime=FakeRuntime(refused=True))
    await env.add()
    out = await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    assert (out.status, out.reason) == ("skipped", "budget_refused")
    assert await env.queued() == []


async def test_an_unauthorized_recipient_costs_no_model_call() -> None:
    env = Env(recipients=False)
    await env.add()
    out = await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    assert (out.status, out.reason) == ("failed", "recipient_not_authorized")
    assert env.runtime.calls == []


async def test_a_revoked_write_schedule_grant_stops_the_run() -> None:
    env = Env(grants={RT: {"send:proactive"}})
    await env.add()
    out = await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    assert (out.status, out.reason) == ("skipped", "not_permitted")
    assert env.runtime.calls == []


async def test_an_unknown_runtime_fails_the_run_without_a_turn() -> None:
    env = Env()
    env.runner = ScheduledTurnRunner(
        env.store,
        {},
        env.sender,
        env.directory,
        deploy_grants=GRANTS,
        limits=env.limits,
    )
    await env.add()
    out = await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    assert (out.status, out.reason) == ("failed", "runtime_unavailable")


async def test_a_failing_turn_is_recorded_and_the_loop_survives() -> None:
    class Boom(FakeRuntime):
        async def run_turn_with_usage(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("model down")

    env = Env(runtime=Boom())
    await env.add()
    out = await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    assert (out.status, out.reason) == ("failed", "turn_failed")
    assert (
        await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    ).status == "idle"


async def test_an_empty_reply_is_not_delivered() -> None:
    env = Env(runtime=FakeRuntime(text="  "))
    await env.add()
    out = await env.runner.run_once(now=UTC_MONDAY_7 + timedelta(minutes=1))
    assert (out.status, out.reason) == ("skipped", "empty_reply")
    assert await env.queued() == []


async def test_nothing_due_is_idle() -> None:
    env = Env()
    await env.add(due=UTC_MONDAY_7 + timedelta(days=1))
    assert (await env.runner.run_once(now=UTC_MONDAY_7)).status == "idle"


# --- subject data store ---------------------------------------------------------


async def test_export_and_delete_cover_only_the_subjects_schedules() -> None:
    from agents_system.subject_data import SubjectRef

    env = Env()
    await env.add()
    await env.add(subject=BOB)
    rows = await env.store.export(SubjectRef(RT, ALICE.key))
    assert [r["prompt"] for r in rows] == ["Send the weekly sales"]
    assert await env.store.delete(SubjectRef(RT, ALICE.key)) == 1
    assert await env.store.export(SubjectRef(RT, ALICE.key)) == []
    assert len(await env.store.export(SubjectRef(RT, BOB.key))) == 1
    # A deleted owner leaves nothing to run.
    assert await env.store.delete(SubjectRef(RT, BOB.key)) == 1
    now = UTC_MONDAY_7 + timedelta(minutes=1)
    assert (await env.runner.run_once(now=now)).status == "idle"
