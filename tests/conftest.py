"""Shared pytest configuration.

D-014 S5 hardening — the Settings security validator fails CLOSED at boot when
adapter runtimes are configured without an adapter API key, or when the Meta
webhook secret is empty (see ``agents_system.config.Settings``). The test suite is
not production: it opts into the insecure/dev mode by default so unrelated
tests (embeddings, RAG, health, etc.) that build ``Settings`` with empty
secrets keep booting. Tests that exercise the security boundary construct
``Settings(..., allow_insecure=False)`` explicitly, which overrides this env
default (init kwargs win over environment variables in pydantic-settings).

This is set at conftest import time — before any test module imports
``agents_system.main`` (whose module-level ``app = create_app()`` would otherwise
trip the fail-closed validator during collection). It CANNOT be an autouse
fixture: fixtures run after collection, and collection is what explodes.

The assignment is unconditional, NOT ``setdefault``. ``setdefault`` is a no-op
against an inherited value, so any environment that exports ALLOW_INSECURE=false
(hardened CI, or a developer checking a production-like config) turned the whole
suite into four collection-time ValidationErrors with nothing pointing at the
cause. The suite's need for the dev mode is not negotiable by the ambient
environment; the security boundary is still tested honestly because those tests
pass ``allow_insecure=False`` as an init kwarg, which outranks the env var.
``tests/test_conftest_contract.py`` pins this.
"""

import inspect
import itertools
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

os.environ["ALLOW_INSECURE"] = "true"

_test_msg_counter = itertools.count(1)

_NEUTRAL_TEST_CATALOG: list[dict[str, Any]] = [
    {"id": "item-001", "name": "Item Alpha", "price": 100.0, "stock": 50},
    {"id": "item-002", "name": "Item Beta", "price": 200.0, "stock": 25},
    {"id": "item-003", "name": "Item Gamma", "price": 150.0, "stock": 30},
]

_NEUTRAL_TEST_CLIENTS: dict[str, dict[str, Any]] = {
    "5491112345678": {
        "client_id": "test-cl-001",
        "name": "Test Client Alpha",
        "phone": "5491112345678",
    },
    "5491187654321": {
        "client_id": "test-cl-002",
        "name": "Test Client Beta",
        "phone": "5491187654321",
    },
}


def _get_neutral_report_catalog() -> dict[str, Any]:
    import importlib

    text_fn = importlib.import_module("sqlalchemy").text
    from agents_system.services.reports import ReportSpec

    return {
        "test_report": ReportSpec(
            name="test_report",
            description="Neutral report for test execution.",
            sql=text_fn("SELECT 1 AS count"),
        )
    }


def fake_catalog_search(inputs: dict[str, Any]) -> dict[str, Any]:
    q = (inputs.get("q") or "").strip().lower()
    if not q:
        return {"results": list(_NEUTRAL_TEST_CATALOG)}
    matches = [item for item in _NEUTRAL_TEST_CATALOG if q in item["name"].lower()]
    return {"results": matches if matches else list(_NEUTRAL_TEST_CATALOG[:2])}


def fake_client_lookup(inputs: dict[str, Any]) -> dict[str, Any]:
    phone = str(inputs.get("phone", ""))
    client = _NEUTRAL_TEST_CLIENTS.get(phone)
    if client:
        return dict(client)
    return {"client_id": None, "name": None, "phone": phone}


def fake_message_sender(inputs: dict[str, Any]) -> dict[str, Any]:
    msg_id = f"test-msg-{next(_test_msg_counter):04d}"
    return {"status": "sent", "message_id": msg_id, "to": inputs.get("to")}


def fake_session_state(inputs: dict[str, Any]) -> dict[str, Any]:
    action = inputs.get("action", "get")
    session_id = inputs.get("session_id", "s-test")
    if action == "set":
        return {"status": "ok", "session_id": session_id}
    return {"session_id": session_id, "data": inputs.get("data", {})}


def build_test_registry(
    settings: Any = None,
    embedder: Any = None,
    bi_engine: Any = None,
    *,
    terminal_policy: Any = None,
    order_writer: Any = None,
    report_catalog: dict[str, Any] | None = None,
    knowledge_base: Any = None,
    summarizer: Any = None,
    escalation_channel: Any = None,
    catalog_connector: Callable[..., Any] | None = None,
) -> Any:
    """Build a ToolRegistry assembled from public platform builders and neutral fakes."""
    from agents_system.connectors.operator import build_operator_tool_specs
    from agents_system.connectors.order_connector import build_order_writer_tool_spec
    from agents_system.connectors.platform_connectors import (
        build_conversation_summarizer_tool_spec,
        build_escalation_notifier_tool_spec,
        build_knowledge_retrieval_tool_spec,
    )
    from agents_system.connectors.report_connector import build_report_tool_spec
    from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec

    registry = ToolRegistry()

    # 1. catalog_search
    registry.register(
        ToolSpec(
            name="catalog_search",
            description="Search the test product catalog. Returns a list of matching products with id, name, price, and stock.",
            required_permissions=("read:catalog",),
            tier=Tier.T1,
            input_schema={
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": "Search query (product name or keyword). Leave empty to return all products.",
                    }
                },
                "required": [],
            },
            connector=catalog_connector or fake_catalog_search,
        )
    )

    # 2. client_lookup
    registry.register(
        ToolSpec(
            name="client_lookup",
            description="Look up a client by phone number. Returns client_id, name, and phone. Use this before creating an order.",
            required_permissions=("read:client_registry",),
            tier=Tier.T1,
            input_schema={
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Client phone number in international format, e.g. 5491112345678",
                    }
                },
                "required": ["phone"],
            },
            connector=fake_client_lookup,
        )
    )

    # 3. order_writer (public platform builder)
    registry.register(build_order_writer_tool_spec(order_writer))

    # 4. message_sender
    registry.register(
        ToolSpec(
            name="message_sender",
            description="Send a message to a phone number.",
            required_permissions=("send:message",),
            tier=Tier.T2,
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient phone number"},
                    "text": {"type": "string", "description": "Message text to send"},
                },
                "required": ["to", "text"],
            },
            connector=fake_message_sender,
        )
    )

    # 5. session_state
    registry.register(
        ToolSpec(
            name="session_state",
            description="Get or set session state data for the current conversation.",
            required_permissions=(),
            tier=Tier.T0,
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]},
                    "session_id": {"type": "string"},
                    "data": {"type": "object"},
                },
                "required": ["action", "session_id"],
            },
            connector=fake_session_state,
        )
    )

    # 6. run_report (public platform builder)
    catalog = (
        report_catalog if report_catalog is not None else _get_neutral_report_catalog()
    )
    registry.register(build_report_tool_spec(bi_engine, catalog))

    # 7. knowledge_retrieval (public platform builder)
    registry.register(build_knowledge_retrieval_tool_spec(knowledge_base))

    # 8. conversation_summarizer (public platform builder)
    registry.register(build_conversation_summarizer_tool_spec(summarizer))

    # 9. escalation_notifier (public platform builder)
    registry.register(build_escalation_notifier_tool_spec(escalation_channel))

    # 10 & 11. use_term, read_file (public platform builder)
    for spec in build_operator_tool_specs(terminal_policy):
        registry.register(spec)

    return registry


class TestRegistryFactory:
    """Generic test-only RegistryFactory assembled from public platform builders."""

    def __init__(
        self,
        *,
        terminal_policy: Any = None,
        order_writer: Any = None,
        report_catalog: dict[str, Any] | None = None,
        knowledge_base: Any = None,
        summarizer: Any = None,
        escalation_channel: Any = None,
        catalog_connector: Callable[..., Any] | None = None,
    ) -> None:
        self.terminal_policy = terminal_policy
        self.order_writer = order_writer
        self.report_catalog = report_catalog
        self.knowledge_base = knowledge_base
        self.summarizer = summarizer
        self.escalation_channel = escalation_channel
        self.catalog_connector = catalog_connector

    def __call__(
        self,
        settings: Any = None,
        embedder: Any = None,
        bi_engine: Any = None,
    ) -> Any:
        return build_test_registry(
            settings=settings,
            embedder=embedder,
            bi_engine=bi_engine,
            terminal_policy=self.terminal_policy,
            order_writer=self.order_writer,
            report_catalog=self.report_catalog,
            knowledge_base=self.knowledge_base,
            summarizer=self.summarizer,
            escalation_channel=self.escalation_channel,
            catalog_connector=self.catalog_connector,
        )


@dataclass
class FakeParticipant:
    """Deterministic, test-only participant conforming to Participant protocol."""

    id: Any = "test-p-001"
    active: bool = True
    name: str = "Test Participant"
    phone_number: str = "+5491123456789"


def fake_normalize_address(raw: str) -> str:
    """Deterministic, test-only address normalizer.

    Rejects empty or non-phone inputs with ValueError.
    Stays deliberately generic without regional quirks.
    """
    if not raw or not isinstance(raw, str):
        raise ValueError("address is empty")
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError("address is empty")
    has_plus = cleaned.startswith("+")
    body = cleaned[1:] if has_plus else cleaned
    sanitized = body.replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
    if not sanitized.isdigit() or len(sanitized) < 7:
        raise ValueError(f"invalid phone: {raw!r}")
    return f"+{sanitized}"


class FakeParticipantDirectory:
    """Deterministic test-only directory implementing ParticipantDirectory."""

    def __init__(
        self,
        participants: dict[str, FakeParticipant | None] | None = None,
        *,
        default_participant: FakeParticipant | None = None,
        resolve_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.participants: dict[str, FakeParticipant | None] = (
            dict(participants) if participants is not None else {}
        )
        self.default_participant = (
            default_participant
            if default_participant is not None
            else FakeParticipant()
        )
        self.resolve_fn = resolve_fn
        self.resolved: list[tuple[Any, str]] = []

    def normalize_address(self, raw: str) -> str:
        return fake_normalize_address(raw)

    async def resolve(self, session: Any, address: str) -> Any:
        self.resolved.append((session, address))
        if self.resolve_fn is not None:
            res = self.resolve_fn(session, address)
            if inspect.isawaitable(res):
                return await res
            return res
        if address in self.participants:
            return self.participants[address]
        return self.default_participant


class FakeConversationRecorder:
    """Deterministic in-memory ConversationRecorder fake with spy support."""

    def __init__(self, spy: Callable[..., Any] | None = None) -> None:
        self.turns: list[dict[str, Any]] = []
        self.spy = spy

    async def record_turn(
        self,
        session: Any,
        *,
        thread_id: str,
        participant_id: Any,
        user_text: str,
        assistant_text: str,
    ) -> None:
        entry: dict[str, Any] = {
            "session": session,
            "thread_id": thread_id,
            "participant_id": participant_id,
            "user_text": user_text,
            "assistant_text": assistant_text,
        }
        self.turns.append(entry)
        if self.spy is not None:
            res = self.spy(
                session,
                thread_id=thread_id,
                participant_id=participant_id,
                user_text=user_text,
                assistant_text=assistant_text,
            )
            if inspect.isawaitable(res):
                await res


def create_test_app(**overrides: Any) -> Any:
    """`create_app` with generic test fixture composition filled in.

    `create_app` has no default registry factory on purpose — the platform
    owns no connectors, so an application must say what it boots with. Most
    tests here do not care which registry that is (they set
    `app.state.runtimes` directly, or never reach a tool at all), so this
    supplies the generic test registry factory.

    A test that IS about composition passes its own arguments instead.
    """
    from agents_system.main import create_app

    kwargs: dict[str, Any] = {
        "registry_factory": build_test_registry,
        "participant_directory": FakeParticipantDirectory(),
        "conversation_recorder": FakeConversationRecorder(),
    }
    kwargs.update(overrides)
    return create_app(**kwargs)
