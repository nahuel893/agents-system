"""Offline tests for the live-eval registry builder (#171).

`build_live_registry` wires REAL reference backends (`ReferenceBackends` plus
the other opt-in ports) instead of `tests/conftest.py::build_test_registry`'s
neutral fakes -- see `docs/platform/reference-backends.md`. These tests use a
fake `AsyncEngine` (the same shape `tests/test_reference_backends.py`'s own
`_ReadOnlyEngine` uses) so no real Postgres connection is required; a fake
chat model stands in for the summarizer's `BaseChatModel` dependency.

The central property under test, per PR #176's review: every STATEFUL
backend must be constructed FRESH by each call, so two calls never share
process-local state -- proven below by rebuilding the registry and observing
that a stateful backend's own counter/store resets rather than continuing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agents_system.evals.live_registry import (
    DEMO_SUMMARY_SESSION_ID,
    build_live_registry,
    build_live_registry_factory,
)
from agents_system.harness.registry import Tier

# ---------------------------------------------------------------------------
# Fake AsyncEngine -- same shape as tests/test_reference_backends.py's own
# _ReadOnlyEngine, kept local so this file has no cross-test-module coupling.
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    _mapping: dict[str, Any]


@dataclass
class _Result:
    rows: list[dict[str, Any]]

    def __iter__(self) -> Any:
        return iter(_Row(row) for row in self.rows)

    def mappings(self) -> "_Result":
        return self

    def all(self) -> list[dict[str, Any]]:
        return self.rows


@dataclass
class _ReadOnlyConnection:
    engine: "_ReadOnlyEngine"

    async def __aenter__(self) -> "_ReadOnlyConnection":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.engine.calls.append((statement, params))
        return _Result(self.engine.row_batches.pop(0))


@dataclass
class _ReadOnlyEngine:
    row_batches: list[list[dict[str, Any]]] = field(default_factory=list)
    calls: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)

    def connect(self) -> _ReadOnlyConnection:
        return _ReadOnlyConnection(self)


def _fake_model() -> FakeMessagesListChatModel:
    return FakeMessagesListChatModel(
        responses=[AIMessage(content="A customer asked about Fernet stock.")]
    )


# ---------------------------------------------------------------------------
# Every expected tool is registered
# ---------------------------------------------------------------------------


def test_build_live_registry_registers_every_expected_tool() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    assert set(registry.names()) == {
        "catalog_search",
        "client_lookup",
        "run_report",
        "message_sender",
        "knowledge_retrieval",
        "conversation_summarizer",
        "escalation_notifier",
        "order_writer",
        "session_state",
        "use_term",
        "read_file",
    }


def test_build_live_registry_catalog_search_is_the_real_reference_backend() -> None:
    """`catalog_search` must come from `ReferenceBackends` (real demo-data
    lookup, `q` required), not the neutral test fake (`q` optional)."""
    registry = build_live_registry(_ReadOnlyEngine(row_batches=[[]]), _fake_model())

    spec = registry.get("catalog_search")

    assert spec.required_permissions == ("read:catalog",)
    assert spec.input_schema["required"] == ["q"]


# ---------------------------------------------------------------------------
# knowledge_retrieval -- seeded known-true / known-absent facts
# ---------------------------------------------------------------------------


async def test_knowledge_retrieval_answers_a_seeded_known_true_fact() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    result = await registry.get("knowledge_retrieval").connector(
        {"q": "delivery zones"}
    )

    assert "error_kind" not in result
    assert result["results"], "expected a hit for a seeded fact"


async def test_knowledge_retrieval_returns_no_hit_for_an_unseeded_fact() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    result = await registry.get("knowledge_retrieval").connector(
        {"q": "warranty policy on electronics"}
    )

    assert "error_kind" not in result
    assert result["results"] == []


# ---------------------------------------------------------------------------
# conversation_summarizer -- pre-seeded demo session
# ---------------------------------------------------------------------------


async def test_conversation_summarizer_succeeds_for_the_pre_seeded_demo_session() -> (
    None
):
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    result = await registry.get("conversation_summarizer").connector(
        {"session_id": DEMO_SUMMARY_SESSION_ID}
    )

    assert "error_kind" not in result
    assert result["summary"]
    assert result["message_count"] == 2


async def test_conversation_summarizer_fails_for_an_unseeded_session() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    result = await registry.get("conversation_summarizer").connector(
        {"session_id": "never-seeded-session"}
    )

    assert result["error_kind"] == "summarization_failed"


# ---------------------------------------------------------------------------
# session_state -- the locally-defined tool (no platform connector exists)
# ---------------------------------------------------------------------------


async def test_session_state_set_then_get_round_trips_within_one_registry() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())
    spec = registry.get("session_state")

    set_result = await spec.connector(
        {"action": "set", "session_id": "s-1", "data": {"topic": "fernet"}}
    )
    get_result = await spec.connector({"action": "get", "session_id": "s-1"})

    assert set_result == {"status": "ok", "session_id": "s-1"}
    assert get_result == {"session_id": "s-1", "data": {"topic": "fernet"}}


async def test_session_state_get_on_an_unset_session_is_empty_not_an_error() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    result = await registry.get("session_state").connector(
        {"action": "get", "session_id": "never-set"}
    )

    assert result == {"session_id": "never-set", "data": {}}


# ---------------------------------------------------------------------------
# escalation_notifier / order_writer -- real reference backends
# ---------------------------------------------------------------------------


async def test_escalation_notifier_logs_never_claims_a_human_was_notified() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    result = await registry.get("escalation_notifier").connector(
        {"reason": "customer_angry", "details": "third failed delivery"}
    )

    assert result["escalation_id"]
    assert result["status"] == "logged"


async def test_order_writer_records_and_the_order_is_retrievable() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    result = await registry.get("order_writer").connector(
        {"client_id": "cl-1", "items": [{"product_id": "ART-009", "qty": 1}]}
    )

    assert "error_kind" not in result
    assert result["order_id"]


# ---------------------------------------------------------------------------
# use_term / read_file -- fail closed with no TerminalPolicy
# ---------------------------------------------------------------------------


async def test_operator_tools_refuse_everything_without_a_terminal_policy() -> None:
    registry = build_live_registry(_ReadOnlyEngine(), _fake_model())

    use_term_result = await registry.get("use_term").connector({"argv": ["ls"]})
    read_file_result = await registry.get("read_file").connector({"path": "x"})

    assert use_term_result["error_kind"] == "not_configured"
    assert read_file_result["error_kind"] == "not_configured"
    assert registry.get("use_term").tier is Tier.T3
    assert registry.get("read_file").tier is Tier.T3


# ---------------------------------------------------------------------------
# Isolation -- every stateful backend is fresh per call (PR #176 review note)
# ---------------------------------------------------------------------------


async def test_build_live_registry_gives_two_calls_unshared_escalation_state() -> None:
    """`LoggingEscalationChannel`'s own counter must reset -- if the second
    call reused the first's channel instance, the second escalation_id would
    be `...000002`, not `...000001` again."""
    registry_a = build_live_registry(_ReadOnlyEngine(), _fake_model())
    registry_b = build_live_registry(_ReadOnlyEngine(), _fake_model())

    result_a = await registry_a.get("escalation_notifier").connector(
        {"reason": "r", "details": "d"}
    )
    result_b = await registry_b.get("escalation_notifier").connector(
        {"reason": "r", "details": "d"}
    )

    assert result_a["escalation_id"] == "logged-escalation-000001"
    assert result_b["escalation_id"] == "logged-escalation-000001"


async def test_build_live_registry_gives_two_calls_unshared_session_state() -> None:
    registry_a = build_live_registry(_ReadOnlyEngine(), _fake_model())
    registry_b = build_live_registry(_ReadOnlyEngine(), _fake_model())

    await registry_a.get("session_state").connector(
        {"action": "set", "session_id": "s-1", "data": {"leaked": True}}
    )
    result_b = await registry_b.get("session_state").connector(
        {"action": "get", "session_id": "s-1"}
    )

    assert result_b == {"session_id": "s-1", "data": {}}


async def test_build_live_registry_gives_two_calls_distinct_registry_objects() -> None:
    engine = _ReadOnlyEngine()
    model = _fake_model()

    registry_a = build_live_registry(engine, model)
    registry_b = build_live_registry(engine, model)

    assert registry_a is not registry_b
    assert registry_a.get("order_writer").connector is not (
        registry_b.get("order_writer").connector
    )


# ---------------------------------------------------------------------------
# build_live_registry_factory
# ---------------------------------------------------------------------------


def test_build_live_registry_factory_returns_a_zero_arg_callable() -> None:
    factory = build_live_registry_factory(_ReadOnlyEngine(), _fake_model())

    registry = factory()

    assert "catalog_search" in registry.names()


async def test_build_live_registry_factory_each_call_is_independent() -> None:
    """Each call to the closure must build a fresh registry with unshared
    stateful backends -- proven the same way as the two direct-call tests
    above, but through the factory a `run_scenario(registry_factory=...)`
    caller actually uses."""
    factory = build_live_registry_factory(_ReadOnlyEngine(), _fake_model())

    registry_one = factory()
    registry_two = factory()

    assert registry_one is not registry_two

    result_one = await registry_one.get("escalation_notifier").connector(
        {"reason": "r", "details": "d"}
    )
    result_two = await registry_two.get("escalation_notifier").connector(
        {"reason": "r", "details": "d"}
    )

    assert result_one["escalation_id"] == "logged-escalation-000001"
    assert result_two["escalation_id"] == "logged-escalation-000001"


async def test_build_live_registry_factory_passes_through_terminal_policy() -> None:
    """A factory built with a `terminal_policy` must wire `use_term`/
    `read_file` to it, not to the fail-closed unconfigured default.

    `read_file` is exercised end to end (pure local file I/O, no subprocess)
    to prove the *root* reaches the connector. `use_term` is probed with a
    command outside the allowlist, which `build_terminal_connector` refuses
    with `command_not_allowed` BEFORE it would ever spawn a sandboxed
    subprocess -- proving the *allowlist* reaches the connector without this
    offline test needing to actually invoke bwrap.
    """
    import pathlib
    import tempfile

    from agents_system.connectors.operator import SandboxPolicy, TerminalPolicy

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "notes.txt").write_text("Sprint status: green.", encoding="utf-8")
        policy = TerminalPolicy(
            root=root,
            allowed_commands=frozenset({"ls"}),
            sandbox=SandboxPolicy(),
        )
        factory = build_live_registry_factory(
            _ReadOnlyEngine(), _fake_model(), terminal_policy=policy
        )

        registry = factory()

        read_result = await registry.get("read_file").connector({"path": "notes.txt"})
        use_term_result = await registry.get("use_term").connector({"argv": ["whoami"]})

        assert read_result["content"] == "Sprint status: green."
        assert use_term_result["error_kind"] == "command_not_allowed"
