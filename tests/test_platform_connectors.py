"""The three platform-generic tools must refuse, not invent (issue #39).

`connectors/platform_stubs.py` answered all three without doing any of the
work. The shapes it returned were the dangerous kind — not errors, but
confident successes:

    escalation_notifier     {"status": "notified", "escalation_id": "esc-0001"}
    conversation_summarizer {"summary": "Customer asked about ..."}
    knowledge_retrieval     {"results": [three hardcoded hits]}

`escalation_notifier` is the worst of the three. It is a `send:` tool whose
entire purpose is to put a human in the loop, it returns "notified" without a
channel behind it, and every role in the taxonomy inherits it — so the one
path a stuck customer has is the one that reports success while nobody is
told.

This is the same failure `run_report` and `order_writer` already refuse: the
platform owns no knowledge base, no conversation store and no escalation
channel, so the only honest answer is that the tool is not configured. These
tests hold that line for each of the three, in both directions: unbound must
refuse, and bound must delegate rather than answer on its own.
"""
from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from agentsys.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)


class _RecordingPort:
    """Stands in for any of the three ports; records what it was asked."""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def search(self, session: Any, *, query: str) -> Any:
        self.calls.append({"query": query})
        return self.result

    async def summarize(
        self, session: Any, *, session_id: str, max_messages: int | None
    ) -> Any:
        self.calls.append({"session_id": session_id, "max_messages": max_messages})
        return self.result

    async def notify(self, session: Any, *, reason: str, details: str) -> Any:
        self.calls.append({"reason": reason, "details": details})
        return self.result


class _FailingPort:
    """A port whose backing system is down, and whose error text is toxic."""

    _BOOM = "connection refused: host=kb.internal password=hunter2"

    async def search(self, session: Any, *, query: str) -> Any:
        raise RuntimeError(self._BOOM)

    async def summarize(
        self, session: Any, *, session_id: str, max_messages: int | None
    ) -> Any:
        raise RuntimeError(self._BOOM)

    async def notify(self, session: Any, *, reason: str, details: str) -> Any:
        raise RuntimeError(self._BOOM)


#: builder, representative inputs, the error_kind an unbound tool must return.
_TOOLS = [
    pytest.param(
        build_knowledge_retrieval_tool_spec,
        {"q": "return policy"},
        "knowledge_not_configured",
        id="knowledge_retrieval",
    ),
    pytest.param(
        build_conversation_summarizer_tool_spec,
        {"session_id": "s-1", "max_messages": 10},
        "summarization_not_configured",
        id="conversation_summarizer",
    ),
    pytest.param(
        build_escalation_notifier_tool_spec,
        {"reason": "customer_angry", "details": "third failed delivery"},
        "escalation_not_configured",
        id="escalation_notifier",
    ),
]


# --- Unbound: refuse, and leave nothing that reads as success ----------------


@pytest.mark.parametrize("builder, inputs, error_kind", _TOOLS)
async def test_an_unbound_tool_refuses_instead_of_fabricating(
    builder: Any, inputs: dict[str, Any], error_kind: str
) -> None:
    """Asserting the error alone is not enough.

    The model reads the whole dict, so a connector returning both an error and
    a success-shaped key would still be read as success. The absence of the
    success shape is the actual requirement.
    """
    result = await builder(None).connector(inputs)

    assert result["error_kind"] == error_kind
    assert result.get("status") != "notified"
    for fabricated in ("escalation_id", "summary", "results", "message_count"):
        assert fabricated not in result


async def test_an_unbound_escalation_tells_the_agent_to_escalate_by_other_means(
) -> None:
    """A failed escalation is the one case where saying nothing is worst.

    The customer asked for a human. If the tool merely reports an error the
    model can silently drop it, which is indistinguishable from the fabricated
    "notified" this replaces. The text has to state that no human was told and
    name what to do instead.
    """
    result = await build_escalation_notifier_tool_spec(None).connector(
        {"reason": "customer_angry", "details": "third failed delivery"}
    )

    error = result["error"].lower()
    assert "not" in error
    assert "human" in error


@pytest.mark.parametrize("builder, inputs, error_kind", _TOOLS)
async def test_an_unbound_tool_names_no_environment_variable(
    builder: Any, inputs: dict[str, Any], error_kind: str
) -> None:
    """This text reaches the model and from there a customer over WhatsApp.

    Operator configuration detail does not belong in a customer's message
    history; the operator gets the specific reason from the startup log.
    """
    result = await builder(None).connector(inputs)

    assert "_URL" not in result["error"]
    assert "env" not in result["error"].lower()


# --- Bound: delegate, never answer on the platform's own authority ----------


async def test_a_bound_knowledge_base_is_asked_and_its_answer_returned() -> None:
    port = _RecordingPort({"results": [{"id": "real-1", "title": "Real", "snippet": "x"}]})

    result = await build_knowledge_retrieval_tool_spec(port).connector(
        {"q": "return policy"}, session="the-session"
    )

    assert result == {"results": [{"id": "real-1", "title": "Real", "snippet": "x"}]}
    assert port.calls == [{"query": "return policy"}]


async def test_a_bound_summarizer_receives_the_session_and_the_cap() -> None:
    """`max_messages` must reach the port.

    Dropping it silently would make every summary cover the whole history,
    which is a different answer than the caller asked for and one they cannot
    detect from the result.
    """
    port = _RecordingPort({"session_id": "s-1", "summary": "real", "message_count": 2})

    await build_conversation_summarizer_tool_spec(port).connector(
        {"session_id": "s-1", "max_messages": 2}
    )

    assert port.calls == [{"session_id": "s-1", "max_messages": 2}]


async def test_a_bound_escalation_channel_receives_reason_and_details() -> None:
    port = _RecordingPort({"escalation_id": "esc-real-7", "status": "notified"})

    result = await build_escalation_notifier_tool_spec(port).connector(
        {"reason": "customer_angry", "details": "third failed delivery"}
    )

    assert port.calls == [
        {"reason": "customer_angry", "details": "third failed delivery"}
    ]
    assert result["escalation_id"] == "esc-real-7"


# --- A raised failure is a result, never an exception ----------------------


@pytest.mark.parametrize("builder, inputs, error_kind", _TOOLS)
async def test_a_port_that_raises_becomes_an_honest_failure(
    builder: Any, inputs: dict[str, Any], error_kind: str
) -> None:
    """`_execute_tools` catches only TimeoutError and PolicyViolation.

    Anything else escapes to the HTTP entry point, and on the WhatsApp path
    that returns 200 to Meta with the customer never hearing back — which they
    cannot tell apart from the request having been handled.
    """
    result = await builder(_FailingPort()).connector(inputs)

    assert result["error_kind"].endswith("_failed")
    assert result.get("status") != "notified"
    assert "summary" not in result


@pytest.mark.parametrize("builder, inputs, error_kind", _TOOLS)
async def test_a_failure_never_leaks_the_exception_text_to_the_model(
    builder: Any, inputs: dict[str, Any], error_kind: str
) -> None:
    """A driver error stringifies host, user and — for a URL DSN — the password."""
    result = await builder(_FailingPort()).connector(inputs)

    rendered = repr(result)
    assert "hunter2" not in rendered
    assert "kb.internal" not in rendered


# --- Escalation confirms, because it is the one with a side effect ---------


@pytest.mark.parametrize(
    "returned",
    [
        pytest.param({"status": "notified"}, id="no-escalation-id"),
        pytest.param({"escalation_id": ""}, id="empty-escalation-id"),
        pytest.param({"escalation_id": "   "}, id="blank-escalation-id"),
        pytest.param({"escalation_id": None}, id="null-escalation-id"),
        pytest.param("notified", id="not-a-dict"),
    ],
)
async def test_an_escalation_without_an_id_is_not_reported_as_notified(
    returned: Any,
) -> None:
    """The same discipline `order_writer` applies to its result (issue #39).

    A channel belonging to the deployment is untrusted input, not an internal
    value. Relaying `{"status": "notified"}` with nothing identifying the
    escalation puts the fabrication back exactly where it was, one boundary
    further out, and the customer is again told a human was reached.

    The two read tools deliberately do NOT get this check: they have no side
    effect to confirm, and an empty result from a real knowledge base is a
    legitimate answer rather than a broken contract.
    """
    result = await build_escalation_notifier_tool_spec(
        _RecordingPort(returned)
    ).connector({"reason": "customer_angry", "details": "third failed delivery"})

    assert result["error_kind"] == "escalation_unconfirmed"
    assert "escalation_id" not in result
    assert result.get("status") != "notified"


async def test_an_unconfirmed_escalation_is_logged_for_the_operator() -> None:
    """The model gets a safe sentence; the operator gets the broken contract."""
    with capture_logs() as logs:
        await build_escalation_notifier_tool_spec(_RecordingPort({})).connector(
            {"reason": "customer_angry", "details": "x"}
        )

    assert any(entry["event"] == "escalation.unconfirmed" for entry in logs)


# --- Validation happens before the port is reached -------------------------


@pytest.mark.parametrize(
    "inputs",
    [
        pytest.param({"details": "x"}, id="no-reason"),
        pytest.param({"reason": "   ", "details": "x"}, id="blank-reason"),
        pytest.param({"reason": "customer_angry"}, id="no-details"),
    ],
)
async def test_an_incomplete_escalation_never_reaches_the_channel(
    inputs: dict[str, Any],
) -> None:
    """A human paged with no reason cannot act, so this is not a formality."""
    port = _RecordingPort({"escalation_id": "should-not-happen"})

    result = await build_escalation_notifier_tool_spec(port).connector(inputs)

    assert result["error_kind"] == "invalid_escalation"
    assert "escalation_id" not in result
    assert port.calls == []


async def test_an_empty_knowledge_query_never_reaches_the_port() -> None:
    """An empty query against a real knowledge base is an unbounded scan."""
    port = _RecordingPort({"results": []})

    result = await build_knowledge_retrieval_tool_spec(port).connector({"q": "  "})

    assert result["error_kind"] == "invalid_query"
    assert port.calls == []
