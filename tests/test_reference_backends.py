"""Reference backends for the platform-generic ports (issue #113 / ADR-002 C.15).

`services.knowledge.KnowledgeBase`, `services.summaries.ConversationSummarizer`,
`services.escalation.EscalationChannel`, and `services.orders.OrderWriter` had
no implementation anywhere in the repository, not even a test fake — every
test exercising the platform-generic tools bound to them necessarily only
exercised the fail-closed path (`test_platform_connectors.py`,
`test_order_writer_connector.py`).

These tests exercise the reference backends `services/reference.py` ships,
through the SAME connectors those two files already hold fail-closed: they
prove the bound path actually works now, and that leaving a backend
unconfigured is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy.exc import SQLAlchemyError
from structlog.testing import capture_logs

from agents_system.connectors.order_connector import build_order_writer_tool_spec
from agents_system.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)
from agents_system.services.reference import (
    InMemoryKnowledgeBase,
    InMemoryOrderWriter,
    KnowledgeDocument,
    LLMConversationSummarizer,
    LoggingEscalationChannel,
    ReferenceBackends,
    TranscriptTurn,
)

# --- KnowledgeBase -----------------------------------------------------------


async def test_in_memory_knowledge_base_retrieves_a_seeded_document() -> None:
    """A role configured with the reference KnowledgeBase gets a real hit,
    instead of the `knowledge_not_configured` fail-closed path."""
    kb = InMemoryKnowledgeBase(
        [
            KnowledgeDocument(
                id="doc-1",
                title="Return policy",
                content="Returns are accepted within 30 days of purchase.",
            )
        ]
    )
    spec = build_knowledge_retrieval_tool_spec(kb)

    result = await spec.connector({"q": "return policy"})

    assert "error_kind" not in result
    assert [hit["id"] for hit in result["results"]] == ["doc-1"]
    assert result["results"][0]["title"] == "Return policy"


async def test_in_memory_knowledge_base_returns_empty_results_on_a_miss() -> None:
    """A miss is a legitimate empty answer, never the fail-closed error — the
    predecessor stub's sin was substituting unrelated hits for exactly this
    case."""
    kb = InMemoryKnowledgeBase(
        [KnowledgeDocument(id="doc-1", title="Return policy", content="...")]
    )
    spec = build_knowledge_retrieval_tool_spec(kb)

    result = await spec.connector({"q": "warranty claims"})

    assert "error_kind" not in result
    assert result["results"] == []


# --- OrderWriter --------------------------------------------------------------


async def test_in_memory_order_writer_records_and_retrieves_an_order() -> None:
    """The reference OrderWriter records the order, replacing
    `order_writing_not_configured`, and the order is retrievable afterward —
    instead of the fail-closed refusal every prior test exercised."""
    writer = InMemoryOrderWriter()
    spec = build_order_writer_tool_spec(writer)

    result = await spec.connector(
        {"client_id": "cl-001", "items": [{"product_id": "prod-1", "qty": 2}]}
    )

    assert "error_kind" not in result
    stored = writer.get_order(result["order_id"])
    assert stored is not None
    assert stored["client_id"] == "cl-001"
    assert stored["items"] == [{"product_id": "prod-1", "qty": 2}]


# --- EscalationChannel ---------------------------------------------------------


async def test_logging_escalation_channel_writes_a_structured_log_not_a_notification() -> (
    None
):
    """It reports having LOGGED the escalation, never that a human was
    notified — there is no paging system behind this reference backend, and
    it must not fabricate one."""
    channel = LoggingEscalationChannel()
    spec = build_escalation_notifier_tool_spec(channel)

    with capture_logs() as logs:
        result = await spec.connector(
            {"reason": "customer_angry", "details": "third failed delivery"}
        )

    assert result["escalation_id"]
    assert result["status"] == "logged"
    assert result["status"] != "notified"
    assert any(
        entry["event"] == "escalation.logged" and entry["reason"] == "customer_angry"
        for entry in logs
    )


# --- ConversationSummarizer -----------------------------------------------------


async def test_llm_conversation_summarizer_reuses_the_roles_configured_model() -> None:
    """The summarizer must call the SAME model instance the role was already
    configured with — no new provider config, per ADR-002 C.15."""
    fake_model = FakeMessagesListChatModel(
        responses=[
            AIMessage(content="Customer asked about delivery and confirmed the order.")
        ]
    )
    summarizer = LLMConversationSummarizer(fake_model)
    summarizer.seed_session(
        "s-1",
        [
            TranscriptTurn(role="user", text="When will my order arrive?"),
            TranscriptTurn(role="assistant", text="Tomorrow by noon."),
        ],
    )
    spec = build_conversation_summarizer_tool_spec(summarizer)

    result = await spec.connector({"session_id": "s-1"})

    assert "error_kind" not in result
    assert result["summary"] == "Customer asked about delivery and confirmed the order."
    assert result["message_count"] == 2


# --- Regression: nothing configured stays fail-closed --------------------------


async def test_reference_backends_are_opt_in_default_stays_fail_closed() -> None:
    """Shipping these reference backends must not change what an unconfigured
    tool does. The per-tool fail-closed suites already pin this in detail;
    this is the cross-cutting regression a reviewer of THIS change should
    see in one place."""
    knowledge = await build_knowledge_retrieval_tool_spec(None).connector({"q": "x"})
    summarizer = await build_conversation_summarizer_tool_spec(None).connector(
        {"session_id": "s-1"}
    )
    escalation = await build_escalation_notifier_tool_spec(None).connector(
        {"reason": "r", "details": "d"}
    )
    order = await build_order_writer_tool_spec(None).connector(
        {"client_id": "cl-1", "items": [{"product_id": "p", "qty": 1}]}
    )

    assert knowledge["error_kind"] == "knowledge_not_configured"
    assert summarizer["error_kind"] == "summarization_not_configured"
    assert escalation["error_kind"] == "escalation_not_configured"
    assert order["error_kind"] == "order_writing_not_configured"


# --- Demo-data reference tools (issue #170) ---------------------------------


@dataclass
class _Row:
    _mapping: dict[str, Any]


@dataclass
class _Result:
    rows: list[dict[str, Any]]

    def __iter__(self) -> Any:
        return iter(_Row(row) for row in self.rows)

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self.rows


@dataclass
class _ReadOnlyConnection:
    engine: _ReadOnlyEngine

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: Any, params: dict[str, Any]) -> _Result:
        self.engine.calls.append((statement, params))
        if self.engine.raises is not None:
            raise self.engine.raises
        return _Result(self.engine.row_batches.pop(0))


@dataclass
class _ReadOnlyEngine:
    row_batches: list[list[dict[str, Any]]] = field(default_factory=list)
    calls: list[tuple[Any, dict[str, Any]]] = field(default_factory=list)
    # issue #145 -- simulates the demo schema being unavailable (a missing
    # `articulos`/`padron_clientes` table, e.g. `boot_live_harness` without
    # `seed_demo=True`): every `execute()` raises this instead of returning
    # a row batch.
    raises: Exception | None = None

    def connect(self) -> _ReadOnlyConnection:
        return _ReadOnlyConnection(self)


async def test_reference_backends_catalog_searches_demo_articulos_lexically() -> None:
    engine = _ReadOnlyEngine(
        row_batches=[[{"sku": "A-42", "description": "Yerba mate"}], [], [], []]
    )
    backends = ReferenceBackends(engine)
    spec = backends.catalog_search_tool_spec()

    hit = await spec.connector({"q": "yerba"})
    miss = await spec.connector({"q": "does not exist"})
    literal_wildcard = await spec.connector({"q": "%_literal"})
    invalid_limit = await spec.connector({"q": "yerba", "limit": True})

    assert spec.required_permissions == ("read:catalog",)
    assert spec.tier.value == "T1"
    assert hit == {
        "results": [{"sku": "A-42", "description": "Yerba mate", "similarity": None}],
        "classification": "ambiguous",
    }
    assert miss == {"results": [], "classification": "no_match"}
    assert literal_wildcard == {"results": [], "classification": "no_match"}
    assert invalid_limit == {"results": [], "classification": "no_match"}
    assert "FROM articulos" in engine.calls[0][0].text
    assert "strpos" in engine.calls[0][0].text
    assert engine.calls[0][1]["query"] == "yerba"
    assert engine.calls[2][1]["query"] == "%_literal"
    assert engine.calls[3][1]["limit"] == 10


async def test_reference_backends_catalog_search_reports_direct_for_an_exact_match() -> (
    None
):
    """#117: an exact SKU or description match (case-insensitive) must report
    a real confidence signal instead of the constant `"ambiguous"` /
    `similarity: None` every hit got before, regardless of how confident it
    actually was. `sales-agent`'s policy treats anything other than a
    confident classification as `confidence_below_threshold` and escalates
    instead of ordering -- see #117 and `tests/test_live_eval_phase1.py`."""
    engine = _ReadOnlyEngine(
        row_batches=[
            [{"sku": "ART-001", "description": "Yerba mate 1kg"}],
            [{"sku": "ART-002", "description": "yerba mate 1kg"}],
        ]
    )
    backends = ReferenceBackends(engine)
    spec = backends.catalog_search_tool_spec()

    exact_sku = await spec.connector({"q": "ART-001"})
    exact_description_case_insensitive = await spec.connector({"q": "Yerba Mate 1KG"})

    assert exact_sku == {
        "results": [
            {"sku": "ART-001", "description": "Yerba mate 1kg", "similarity": 1.0}
        ],
        "classification": "direct",
    }
    assert exact_description_case_insensitive == {
        "results": [
            {"sku": "ART-002", "description": "yerba mate 1kg", "similarity": 1.0}
        ],
        "classification": "direct",
    }


async def test_reference_backends_catalog_search_stays_ambiguous_for_partial_matches() -> (
    None
):
    """#117 acceptance criterion: a genuinely ambiguous query (several
    partial, non-exact matches) must still report `"ambiguous"` -- the fix is
    a real signal for a confident hit, not a change that inflates every
    result's confidence."""
    engine = _ReadOnlyEngine(
        row_batches=[
            [
                {"sku": "ART-001", "description": "Yerba mate 1kg"},
                {"sku": "ART-002", "description": "Yerba mate 500g"},
            ]
        ]
    )
    backends = ReferenceBackends(engine)
    spec = backends.catalog_search_tool_spec()

    result = await spec.connector({"q": "yerba mate"})

    assert result == {
        "results": [
            {"sku": "ART-001", "description": "Yerba mate 1kg", "similarity": None},
            {"sku": "ART-002", "description": "Yerba mate 500g", "similarity": None},
        ],
        "classification": "ambiguous",
    }


async def test_reference_backends_catalog_search_direct_classification_keeps_non_exact_rows_null() -> (
    None
):
    """Issue #146 review: `classification` is `"direct"` for the WHOLE
    response the moment ANY row is an exact match -- it is not a per-row
    property. A non-exact row returned alongside that exact one in the SAME
    response must still report its own `similarity: None`, not inherit the
    response's overall confidence (docs/platform/reference-backends.md)."""
    engine = _ReadOnlyEngine(
        row_batches=[
            [
                {"sku": "ART-001", "description": "Yerba mate 1kg"},
                {"sku": "ART-002", "description": "Yerba mate 500g"},
            ]
        ]
    )
    backends = ReferenceBackends(engine)
    spec = backends.catalog_search_tool_spec()

    result = await spec.connector({"q": "ART-001"})

    assert result == {
        "results": [
            {"sku": "ART-001", "description": "Yerba mate 1kg", "similarity": 1.0},
            {"sku": "ART-002", "description": "Yerba mate 500g", "similarity": None},
        ],
        "classification": "direct",
    }


async def test_reference_backends_lookup_existing_demo_client_or_returns_no_hit() -> (
    None
):
    known_phone = "+5491100000042"
    unknown_phone = "+5491100009999"
    engine = _ReadOnlyEngine(
        row_batches=[[{"client_id": 42, "name": "Almacen Norte"}], []]
    )
    backends = ReferenceBackends(engine)
    spec = backends.client_lookup_tool_spec()

    hit = await spec.connector({"phone": known_phone})
    miss = await spec.connector({"phone": unknown_phone})

    assert spec.name == "client_lookup"
    assert spec.required_permissions == ("read:client_registry",)
    assert spec.tier.value == "T1"
    assert spec.input_schema == {
        "type": "object",
        "properties": {
            "phone": {
                "type": "string",
                "description": (
                    "Demo customer phone: +549110000 followed by a four-digit customer ID, e.g. +5491100000001"
                ),
            }
        },
        "required": ["phone"],
    }
    assert hit == {
        "client_id": "42",
        "name": "Almacen Norte",
        "phone": known_phone,
    }
    assert miss == {"client_id": None, "name": None, "phone": unknown_phone}
    assert "FROM padron_clientes" in engine.calls[0][0].text
    assert engine.calls[0][1] == {"client_id": 42}


async def test_reference_backends_reject_noncanonical_or_mismatched_demo_phones() -> (
    None
):
    invalid_engine = _ReadOnlyEngine(row_batches=[])
    invalid = (
        await ReferenceBackends(invalid_engine)
        .client_lookup_tool_spec()
        .connector({"phone": "+54911000042"})
    )
    mismatched_engine = _ReadOnlyEngine(
        row_batches=[[{"client_id": 43, "name": "Different client"}]]
    )
    mismatched = (
        await ReferenceBackends(mismatched_engine)
        .client_lookup_tool_spec()
        .connector({"phone": "+5491100000042"})
    )

    assert invalid == {"client_id": None, "name": None, "phone": "+54911000042"}
    assert invalid_engine.calls == []
    assert mismatched == {
        "client_id": None,
        "name": None,
        "phone": "+5491100000042",
    }


async def test_reference_backends_report_delegates_to_closed_sales_catalog() -> None:
    engine = _ReadOnlyEngine(row_batches=[[]])
    spec = ReferenceBackends(engine).run_report_tool_spec()

    empty = await spec.connector({"report": "sales_by_month"})
    unknown = await spec.connector({"report": "invented_report"})
    unbound = (
        await ReferenceBackends(None)
        .run_report_tool_spec()
        .connector({"report": "sales_by_month"})
    )

    assert spec.name == "run_report"
    assert empty["empty_result"] is True
    assert empty["rows"] == []
    assert "Unknown report" in unknown["error"]
    assert unbound["error_kind"] == "bi_not_configured"


async def test_reference_backends_record_messages_only_for_existing_demo_clients() -> (
    None
):
    known_phone = "+5491100000042"
    unknown_phone = "+5491100009999"
    engine = _ReadOnlyEngine(
        row_batches=[[{"client_id": 42, "name": "Almacen Norte"}], []]
    )
    backends = ReferenceBackends(engine)
    spec = backends.message_sender_tool_spec()

    recorded = await spec.connector({"to": known_phone, "text": "Pedido listo"})
    unknown = await spec.connector({"to": unknown_phone, "text": "Pedido listo"})

    assert spec.name == "message_sender"
    assert spec.required_permissions == ("send:message",)
    assert spec.tier.value == "T2"
    assert spec.input_schema == {
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient phone number"},
            "text": {"type": "string", "description": "Message text to send"},
        },
        "required": ["to", "text"],
    }
    assert recorded == {
        "status": "recorded",
        "message_id": "recorded-message-000001",
        "to": known_phone,
    }
    assert "sent" not in recorded
    assert "delivered" not in recorded
    assert unknown == {"status": "not_found", "to": unknown_phone}
    ledger = backends.recorded_messages
    assert ledger == [
        {
            "message_id": "recorded-message-000001",
            "to": known_phone,
            "text": "Pedido listo",
        }
    ]
    ledger[0]["text"] = "outside mutation"
    ledger.append({"message_id": "outside-copy", "to": "x", "text": "x"})
    assert backends.recorded_messages == [
        {
            "message_id": "recorded-message-000001",
            "to": known_phone,
            "text": "Pedido listo",
        }
    ]


async def test_reference_backends_fail_closed_when_no_readonly_engine_is_opted_in() -> (
    None
):
    backends = ReferenceBackends(None)

    empty_query = await backends.catalog_search_tool_spec().connector({"q": ""})
    unbound_catalog = await backends.catalog_search_tool_spec().connector(
        {"q": "yerba"}
    )
    unbound_client = await backends.client_lookup_tool_spec().connector(
        {"phone": "+5491100000042"}
    )
    unbound_message = await backends.message_sender_tool_spec().connector(
        {"to": "+5491100000042", "text": "Pedido listo"}
    )

    assert empty_query == {"results": [], "classification": "no_match"}
    assert unbound_catalog["error_kind"] == "reference_data_not_configured"
    assert unbound_client["error_kind"] == "reference_data_not_configured"
    assert unbound_message["error_kind"] == "reference_data_not_configured"
    assert backends.recorded_messages == []


# ---------------------------------------------------------------------------
# issue #145 -- a demo schema that is CONNECTED but missing its tables (e.g.
# `boot_live_harness` without `seed_demo=True`) must degrade the same way an
# unset `readonly_engine` does, never crash the whole turn with a raw SQL
# exception. Reproduced live: `EVAL_PROVIDER=openai_compatible` real-model
# load runs saw every `catalog_search` call fail with
# `asyncpg.exceptions.UndefinedTableError: relation "articulos" does not
# exist`, an unhandled exception that failed the turn and drove the
# `services/outbox.py` retry storm this issue reports as "no error is ever
# returned" / a real-model run barely completing under load.
# ---------------------------------------------------------------------------


async def test_catalog_search_reports_not_configured_when_demo_schema_is_missing() -> (
    None
):
    engine = _ReadOnlyEngine(
        raises=SQLAlchemyError('relation "articulos" does not exist')
    )
    backends = ReferenceBackends(engine)

    with capture_logs() as logs:
        result = await backends.catalog_search_tool_spec().connector({"q": "yerba"})

    assert result["error_kind"] == "reference_data_not_configured"
    assert any(
        entry.get("event") == "reference_backends.demo_schema_unavailable"
        for entry in logs
    )


async def test_client_lookup_reports_not_configured_when_demo_schema_is_missing() -> (
    None
):
    engine = _ReadOnlyEngine(
        raises=SQLAlchemyError('relation "padron_clientes" does not exist')
    )
    backends = ReferenceBackends(engine)

    result = await backends.client_lookup_tool_spec().connector(
        {"phone": "+5491100000042"}
    )

    assert result["error_kind"] == "reference_data_not_configured"


async def test_message_sender_reports_not_configured_when_demo_schema_is_missing() -> (
    None
):
    """`message_sender` validates its recipient through `_lookup_client` --
    the same missing-schema degradation must propagate through it too."""
    engine = _ReadOnlyEngine(
        raises=SQLAlchemyError('relation "padron_clientes" does not exist')
    )
    backends = ReferenceBackends(engine)

    result = await backends.message_sender_tool_spec().connector(
        {"to": "+5491100000042", "text": "hola"}
    )

    assert result["error_kind"] == "reference_data_not_configured"
    assert backends.recorded_messages == []


async def test_catalog_search_still_reports_a_genuine_no_match_when_schema_is_fine() -> (
    None
):
    """A working demo schema that legitimately has no matching rows must
    still answer `no_match` -- the new degradation path must never mask a
    real, honest empty result as a config problem."""
    engine = _ReadOnlyEngine(row_batches=[[]])
    backends = ReferenceBackends(engine)

    result = await backends.catalog_search_tool_spec().connector({"q": "nope"})

    assert result == {"results": [], "classification": "no_match"}
