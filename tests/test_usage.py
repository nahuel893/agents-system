"""Per-end-user usage ledger and budget caps (ADR-011 part 5, #252)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from prometheus_client import CollectorRegistry

from agents_system.agent.graph import AgentRuntime
from agents_system.config import ModelPrice
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.loader import AgentDefinition
from agents_system.harness.registry import Tier, ToolSpec
from agents_system.observability.metrics import build_metrics
from agents_system.scope import InMemoryScopeProvider, assert_subject_isolation
from agents_system.subject import TurnActor, TurnSubject
from agents_system.usage import (
    BudgetCaps,
    InMemoryUsageLedger,
    UsageRecord,
    parse_budgets,
    window_starts,
)

RT = "support"
ALICE = TurnSubject.for_participant("alice", channel="whatsapp", address="+54911")
BOB = TurnSubject.for_participant("bob")
ACTOR = TurnActor(kind="channel", id="whatsapp")
PRICES = {"fake": ModelPrice(input_per_million=10_000.0, output_per_million=0.0)}


def _usage(tokens: int) -> dict[str, int]:
    return {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}


class _Model(FakeMessagesListChatModel):
    calls: int = 0
    model_name: str = "fake"

    def _generate(self, *a: Any, **kw: Any) -> Any:
        self.calls += 1
        return super()._generate(*a, **kw)

    def bind_tools(self, tools: Any, **kw: Any) -> Any:  # type: ignore[override]
        return self


def _runtime(
    responses: list[AIMessage],
    *,
    caps: BudgetCaps | None,
    ledger: InMemoryUsageLedger | None = None,
    prices: Any = None,
    tools: tuple[ToolSpec, ...] = (),
    metrics: Any = None,
) -> tuple[AgentRuntime, _Model, InMemoryUsageLedger]:
    definition = AgentDefinition(
        role_name="r",
        version="1.0",
        deployment=None,
        system_prompt="p",
        tools=(),
        skills=(),
        context={},
        permissions=(),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )
    equipped = EquippedRuntime(
        definition=definition,
        system_prompt="p",
        tools=tools,
        denied_tools=(),
        skills=(),
    )
    model = _Model(responses=responses)
    ledger = ledger or InMemoryUsageLedger(InMemoryScopeProvider())
    runtime = AgentRuntime(
        equipped,
        model,
        runtime_id=RT,
        model_prices=PRICES if prices is None else prices,
        budget=caps,
        usage_ledger=ledger,
        metrics=metrics,
    )
    return runtime, model, ledger


async def _turn(runtime: AgentRuntime, subject: TurnSubject | None) -> Any:
    return await runtime.run_turn_with_usage(
        [HumanMessage(content="hi")], "s1", subject=subject, actor=ACTOR
    )


def test_parse_budgets_reads_the_deploy_grants_shape() -> None:
    parsed = parse_budgets(
        {RT: {"daily_usd": 1.5, "monthly_tokens": 1000, "message": "slow down"}}
    )
    assert parsed[RT] == BudgetCaps(
        daily_usd=1.5, monthly_tokens=1000, message="slow down"
    )


@pytest.mark.parametrize(
    "bad", [{"daily_usd": 0}, {"daily_usd": -1}, {"nope": 1}, {}, {"daily_tokens": 1.5}]
)
def test_parse_budgets_rejects_a_bad_cap(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        parse_budgets({RT: bad})


def test_window_starts_are_utc_day_and_month() -> None:
    day, month = window_starts(datetime(2026, 3, 15, 13, 5, tzinfo=UTC))
    assert day == datetime(2026, 3, 15, tzinfo=UTC)
    assert month == datetime(2026, 3, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_ledger_isolates_subjects() -> None:
    ledger = InMemoryUsageLedger(InMemoryScopeProvider())

    async def write(scope: Any, value: str) -> None:
        await ledger.record(
            UsageRecord(
                runtime_id=scope.runtime_id,
                subject_key=scope.subject_key,
                actor=value,
            )
        )

    async def read_all(scope: Any) -> list[str]:
        return [
            r.actor or ""
            for r in await ledger.query(scope.runtime_id, scope.subject_key)
        ]

    await assert_subject_isolation(
        ledger.provider, write=write, read_all=read_all, runtime_id=RT
    )


@pytest.mark.asyncio
async def test_two_users_of_one_runtime_are_accounted_separately() -> None:
    runtime, _, ledger = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))] * 3, caps=None
    )
    await _turn(runtime, ALICE)
    await _turn(runtime, ALICE)
    await _turn(runtime, BOB)
    since = datetime(2000, 1, 1, tzinfo=UTC)
    alice = await ledger.totals(RT, ALICE.key, since=since)
    bob = await ledger.totals(RT, BOB.key, since=since)
    assert (alice.tokens, alice.turns) == (20, 2)
    assert (bob.tokens, bob.turns) == (10, 1)
    record = (await ledger.query(RT, ALICE.key))[0]
    assert record.runtime_id == RT and record.subject_key == ALICE.key
    assert record.actor == "channel:whatsapp"
    assert "+54911" not in repr(record)


@pytest.mark.asyncio
async def test_a_turn_without_a_subject_is_not_recorded() -> None:
    runtime, _, ledger = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))], caps=None
    )
    await _turn(runtime, None)
    assert await ledger.query(RT, "participant:alice") == []


@pytest.mark.asyncio
async def test_user_over_the_daily_cap_is_refused_before_any_model_call() -> None:
    caps = BudgetCaps(daily_tokens=15, message="Limit reached.")
    runtime, model, _ = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))] * 4, caps=caps
    )
    await _turn(runtime, ALICE)  # 10 of 15
    await _turn(runtime, ALICE)  # 20 >= 15: next one is refused
    calls_before = model.calls
    refused = await _turn(runtime, ALICE)
    assert refused.budget_refused is True
    assert model.calls == calls_before
    assert refused.messages[-1].content == "Limit reached."
    assert refused.usage.model_calls == 0
    other = await _turn(runtime, BOB)
    assert other.budget_refused is False
    assert model.calls == calls_before + 1


@pytest.mark.asyncio
async def test_a_refused_turn_is_audited_without_the_address(monkeypatch: Any) -> None:
    seen: list[dict[str, Any]] = []
    from agents_system.agent import graph

    monkeypatch.setattr(
        graph, "_emit", lambda name, **kw: seen.append({"name": name, **kw})
    )
    caps = BudgetCaps(daily_tokens=1)
    runtime, _, _ = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))] * 2, caps=caps
    )
    await _turn(runtime, ALICE)
    await _turn(runtime, ALICE)
    events = [e for e in seen if e["name"] == "record_budget_refused"]
    assert len(events) == 1
    assert events[0]["cap"] == "daily_tokens"
    assert "+54911" not in str(events)
    from agents_system.audit.recorder import record_budget_refused

    event = await record_budget_refused(
        None, runtime_id=RT, cap="daily_tokens", limit=1, used=10
    )
    assert event.event_type == "budget_refused"


@pytest.mark.asyncio
async def test_a_tool_loop_overshoots_by_at_most_one_model_call() -> None:
    async def ping(inputs: dict, *, session: Any = None) -> dict:
        return {"ok": True}

    spec = ToolSpec(name="ping", required_permissions=(), connector=ping, tier=Tier.T1)
    call = {"name": "ping", "args": {}, "id": "c1"}
    caps = BudgetCaps(daily_tokens=100)
    runtime, model, ledger = _runtime(
        [
            AIMessage(content="", tool_calls=[call], usage_metadata=_usage(100)),
            AIMessage(content="never", usage_metadata=_usage(100)),
        ],
        caps=caps,
        tools=(spec,),
    )
    result = await _turn(runtime, ALICE)
    assert result.budget_refused is True
    assert model.calls == 1
    totals = await ledger.totals(RT, ALICE.key, since=datetime(2000, 1, 1, tzinfo=UTC))
    assert totals.tokens == 100
    assert (await ledger.query(RT, ALICE.key))[0].tool_calls == 1


@pytest.mark.asyncio
async def test_usd_cap_uses_the_priced_cost() -> None:
    caps = BudgetCaps(daily_usd=0.05)
    # 10_000 USD per million input tokens: 10 tokens cost 0.1 USD.
    runtime, model, _ = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))] * 2, caps=caps
    )
    await _turn(runtime, ALICE)
    refused = await _turn(runtime, ALICE)
    assert refused.budget_refused is True and model.calls == 1


@pytest.mark.asyncio
async def test_unpriced_model_is_counted_in_tokens_and_flagged() -> None:
    runtime, _, ledger = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))], caps=None, prices={}
    )
    await _turn(runtime, ALICE)
    record = (await ledger.query(RT, ALICE.key))[0]
    assert record.total_tokens == 10
    assert record.cost_usd is None and record.priced is False
    totals = await ledger.totals(RT, ALICE.key, since=datetime(2000, 1, 1, tzinfo=UTC))
    assert totals.tokens == 10 and totals.unpriced_turns == 1


def test_usd_cap_on_an_unpriced_model_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="price"):
        _runtime([AIMessage(content="a")], caps=BudgetCaps(daily_usd=1.0), prices={})


@pytest.mark.asyncio
async def test_usd_cap_rejects_an_unpriced_model_id_override() -> None:
    runtime, model, ledger = _runtime(
        [AIMessage(content="paid", usage_metadata=_usage(10))] * 3,
        caps=BudgetCaps(daily_usd=0.05),
    )
    with pytest.raises(ValueError, match="price"):
        await runtime.run_turn_with_usage(
            [HumanMessage(content="hi")],
            "s1",
            subject=ALICE,
            model_id="unpriced-override",
        )
    assert model.calls == 0
    totals = await ledger.totals(RT, ALICE.key, since=datetime(2000, 1, 1, tzinfo=UTC))
    assert totals.unpriced_turns == 0


def test_caps_without_a_ledger_fail_at_construction() -> None:
    runtime_args = _runtime([AIMessage(content="a")], caps=None)[0]
    assert runtime_args is not None
    with pytest.raises(ValueError, match="ledger"):
        AgentRuntime(
            runtime_args._equipped,
            _Model(responses=[AIMessage(content="a")]),
            runtime_id=RT,
            budget=BudgetCaps(daily_tokens=1),
        )


@pytest.mark.asyncio
async def test_metrics_gain_actor_kind_and_never_a_subject_label() -> None:
    metrics = build_metrics(CollectorRegistry())
    runtime, _, _ = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))], caps=None, metrics=metrics
    )
    await _turn(runtime, ALICE)
    sample = next(
        s
        for family in metrics.registry.collect()
        for s in family.samples
        if s.name == "agent_turns_total"
    )
    assert sample.labels["actor_kind"] == "channel"
    assert "participant:alice" not in str(list(metrics.registry.collect()))


@pytest.mark.asyncio
async def test_a_ledger_write_failure_does_not_fail_the_turn() -> None:
    runtime, _, ledger = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))], caps=None
    )

    async def boom(record: UsageRecord) -> None:
        raise RuntimeError("db down")

    ledger.record = boom  # type: ignore[method-assign]
    result = await _turn(runtime, ALICE)
    assert result.messages[-1].content == "a"


@pytest.mark.asyncio
async def test_store_exports_and_deletes_one_subjects_usage() -> None:
    from agents_system.subject_data import SubjectRef

    runtime, _, ledger = _runtime(
        [AIMessage(content="a", usage_metadata=_usage(10))] * 2, caps=None
    )
    await _turn(runtime, ALICE)
    await _turn(runtime, BOB)
    exported = await ledger.export(SubjectRef(RT, ALICE.key))
    assert len(exported) == 1 and exported[0]["total_tokens"] == 10
    assert ledger.tables == frozenset({"subject_usage"})
    assert await ledger.delete(SubjectRef(RT, ALICE.key)) == 1
    assert await ledger.query(RT, ALICE.key) == []
    assert len(await ledger.query(RT, BOB.key)) == 1


def test_user_budgets_setting_parses_like_deploy_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents_system.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv(
        "USER_BUDGETS", '{"support": {"daily_usd": 2, "monthly_tokens": 900}}'
    )
    try:
        assert get_settings().user_budgets["support"] == BudgetCaps(
            daily_usd=2.0, monthly_tokens=900
        )
        monkeypatch.setenv("USER_BUDGETS", '{"support": {"daily_usd": 0}}')
        get_settings.cache_clear()
        with pytest.raises(ValueError):
            get_settings()
    finally:
        get_settings.cache_clear()


def test_budgets_without_a_scope_provider_fail_the_boot() -> None:
    from agents_system.main import _build_usage_ledger
    from agents_system.subject_data import SubjectDataRegistry

    with pytest.raises(ValueError, match="scope_provider_factory"):
        _build_usage_ledger(None, SubjectDataRegistry(), {RT: BudgetCaps(daily_usd=1)})
    assert _build_usage_ledger(None, SubjectDataRegistry(), {}) is None


def test_the_ledger_is_registered_as_a_subject_data_store() -> None:
    from agents_system.main import _build_usage_ledger
    from agents_system.subject_data import SubjectDataRegistry

    registry = SubjectDataRegistry()
    ledger = _build_usage_ledger(InMemoryScopeProvider(), registry, {})
    assert registry.stores["usage"] is ledger


@pytest.mark.asyncio
async def test_operator_usage_route_reports_daily_and_monthly_totals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from conftest import create_test_app
    from fastapi.testclient import TestClient

    from agents_system.config import get_settings

    monkeypatch.setenv("OPERATOR_API_KEY", "operator-secret")
    get_settings.cache_clear()
    try:
        app = create_test_app()
        ledger = InMemoryUsageLedger(InMemoryScopeProvider())
        await ledger.record(
            UsageRecord(runtime_id=RT, subject_key="participant:1", total_tokens=7)
        )
        await ledger.record(
            UsageRecord(
                runtime_id=RT, subject_key="participant:1", total_tokens=3, cost_usd=0.5
            )
        )
        app.state.usage_ledger = ledger
        client = TestClient(app)
        body = {"runtime_id": RT, "subject_key": "participant:1"}
        auth = {"Authorization": "Bearer operator-secret"}
        reply = client.post("/operator/subjects/usage", json=body, headers=auth)
        assert reply.status_code == 200
        data = reply.json()
        assert data["daily"]["tokens"] == 10 and data["monthly"]["turns"] == 2
        assert data["daily"]["unpriced_turns"] == 1
        other = {**body, "subject_key": "participant:2"}
        empty = client.post("/operator/subjects/usage", json=other, headers=auth)
        assert empty.json()["daily"]["tokens"] == 0
        app.state.usage_ledger = None
        none = client.post("/operator/subjects/usage", json=body, headers=auth)
        assert none.status_code == 409
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Review fixes on PR #265
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("name", ["daily_usd", "monthly_usd"])
def test_parse_budgets_rejects_a_non_finite_usd_cap(name: str, bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        parse_budgets({RT: {name: bad}})


def test_budget_caps_objects_are_validated_too() -> None:
    for caps in (
        BudgetCaps(daily_usd=float("nan")),
        BudgetCaps(monthly_usd=float("inf")),
        BudgetCaps(daily_tokens=0),
        BudgetCaps(),
    ):
        with pytest.raises(ValueError):
            parse_budgets({RT: caps})
    good = BudgetCaps(daily_usd=1.0)
    assert parse_budgets({RT: good})[RT] == good


def test_the_runtime_refuses_a_non_finite_cap_at_construction() -> None:
    with pytest.raises(ValueError, match="finite"):
        _runtime([AIMessage(content="a")], caps=BudgetCaps(daily_usd=float("nan")))


@pytest.mark.asyncio
async def test_a_mid_turn_refusal_is_a_budget_refused_outcome_and_is_saved() -> None:
    from langgraph.checkpoint.memory import InMemorySaver

    async def ping(inputs: dict, *, session: Any = None) -> dict:
        return {"ok": True}

    spec = ToolSpec(name="ping", required_permissions=(), connector=ping, tier=Tier.T1)
    call = {"name": "ping", "args": {}, "id": "c1"}
    metrics = build_metrics(CollectorRegistry())
    runtime, model, _ = _runtime(
        [
            AIMessage(content="", tool_calls=[call], usage_metadata=_usage(100)),
            AIMessage(content="never", usage_metadata=_usage(100)),
        ],
        caps=BudgetCaps(daily_tokens=100),
        tools=(spec,),
        metrics=metrics,
    )
    saver = InMemorySaver()
    runtime._checkpointer = saver  # type: ignore[attr-defined]
    result = await runtime.run_turn_with_usage(
        [HumanMessage(content="hi")],
        "s1",
        thread_id="t1",
        subject=ALICE,
        actor=ACTOR,
    )
    assert result.budget_refused is True and model.calls == 1
    outcomes = [
        s.labels["outcome"]
        for family in metrics.registry.collect()
        for s in family.samples
        if s.name == "agent_turns_total"
    ]
    assert outcomes == ["budget_refused"]
    assert saver.get_tuple({"configurable": {"thread_id": "t1"}}) is not None


class _FormatErrorThenSpendModel(_Model):
    """First call spends against the ledger then fails with a tool-format error."""

    spend: Any = None

    @property
    def bound(self) -> Any:
        return self

    async def ainvoke(self, *a: Any, **kw: Any) -> AIMessage:  # type: ignore[override]
        if not getattr(self, "_failed", False):
            object.__setattr__(self, "_failed", True)
            await self.spend()
            raise ValueError("tool_use_failed: malformed tool call")
        self.calls += 1
        return AIMessage(content="fallback")


@pytest.mark.asyncio
async def test_the_tool_format_fallback_rechecks_the_cap() -> None:
    from agents_system.agent.graph import AgentRuntime as _AR

    runtime, _, ledger = _runtime(
        [AIMessage(content="x")], caps=BudgetCaps(daily_tokens=100)
    )
    model = _FormatErrorThenSpendModel(responses=[])

    async def spend() -> None:
        await ledger.record(
            UsageRecord(runtime_id=RT, subject_key=ALICE.key, total_tokens=500)
        )

    model.spend = spend
    runtime = _AR(
        runtime._equipped,
        model,
        runtime_id=RT,
        model_prices=PRICES,
        budget=BudgetCaps(daily_tokens=100),
        usage_ledger=ledger,
    )
    result = await _turn(runtime, ALICE)
    assert result.budget_refused is True
    assert model.calls == 0  # the fallback request was never issued
    assert result.usage.model_calls == 1


def test_a_second_app_does_not_reuse_the_first_apps_ledger_or_registry() -> None:
    from agents_system.main import _build_usage_ledger
    from agents_system.subject_data import DEFAULT_REGISTRY

    pytest.importorskip("sqlalchemy")
    from agents_system.services import usage_postgres  # noqa: F401

    registry = DEFAULT_REGISTRY.copy()
    ledger = _build_usage_ledger(InMemoryScopeProvider(), registry, {})
    # The probe: the placeholder in the default registry must not win.
    assert registry.stores["usage"] is ledger
    assert isinstance(ledger, InMemoryUsageLedger)


def test_a_conflicting_usage_registration_is_rejected() -> None:
    from agents_system.main import _build_usage_ledger
    from agents_system.subject_data import SubjectDataRegistry

    class Custom:
        name = "usage"
        tables = frozenset({"subject_usage"})

    registry = SubjectDataRegistry()
    registry.register(Custom())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="usage"):
        _build_usage_ledger(InMemoryScopeProvider(), registry, {})


def test_binding_a_ledger_leaves_the_registry_it_was_copied_from_alone() -> None:
    from agents_system.main import _build_usage_ledger
    from agents_system.subject_data import DEFAULT_REGISTRY

    pytest.importorskip("sqlalchemy")
    from agents_system.services import usage_postgres

    before = dict(DEFAULT_REGISTRY.stores)
    assert before["usage"] is usage_postgres.USAGE_LEDGER
    _build_usage_ledger(InMemoryScopeProvider(), DEFAULT_REGISTRY.copy(), {})
    assert dict(DEFAULT_REGISTRY.stores) == before


def test_a_postgres_provider_gets_its_own_ledger_registered_in_the_app_registry() -> (
    None
):
    from agents_system.main import _build_usage_ledger
    from agents_system.subject_data import DEFAULT_REGISTRY

    pytest.importorskip("sqlalchemy")
    from agents_system.services import usage_postgres

    class _Provider:  # stands in for a PostgresScopeProvider
        pass

    registry = DEFAULT_REGISTRY.copy()
    first = _build_usage_ledger(_Provider(), registry, {})  # type: ignore[arg-type]
    assert first is not usage_postgres.USAGE_LEDGER
    assert registry.stores["usage"] is first
    assert usage_postgres.USAGE_LEDGER.unbound
    # A second app's own copy is independent of the first.
    other = DEFAULT_REGISTRY.copy()
    second = _build_usage_ledger(_Provider(), other, {})  # type: ignore[arg-type]
    assert other.stores["usage"] is second and second is not first
