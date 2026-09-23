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

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from structlog.testing import capture_logs

from agentsys.connectors.order_connector import build_order_writer_tool_spec
from agentsys.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)
from agentsys.services.reference import (
    InMemoryKnowledgeBase,
    InMemoryOrderWriter,
    KnowledgeDocument,
    LLMConversationSummarizer,
    LoggingEscalationChannel,
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


async def test_logging_escalation_channel_writes_a_structured_log_not_a_notification() -> None:
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
