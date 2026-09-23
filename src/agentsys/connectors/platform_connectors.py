"""The three platform-generic connectors, fail-closed (issue #39).

Replaces `connectors/platform_stubs.py`, which answered all three tools
without doing any of the work and returned the dangerous shape: not an error,
a confident success.

    escalation_notifier     {"status": "notified", "escalation_id": "esc-0001"}
    conversation_summarizer {"summary": "Customer asked about ..."}
    knowledge_retrieval     {"results": [three hardcoded hits]}

`escalation_notifier` was the worst. It is a `send:` tool whose whole purpose
is to put a human in the loop, every role in the taxonomy inherits it, and it
reported "notified" with no channel behind it — so the single path a stuck
customer has was the one that lied about working.

The shape here is `report_connector`'s and `order_connector`'s, for the same
reason: each tool always EXISTS, because the generic role manifests name them
and a tool a manifest names but the registry lacks makes the whole role
unbuildable via `InjectionError`. Whether a real system sits behind it is a
runtime fact the tool reports, never one it papers over.

Each error text is written for the model, which is the only reader: it states
the outcome, forbids the specific fabrication, and gives a next action. None
of them names an environment variable — this text can reach a customer over
WhatsApp, and operator configuration detail does not belong there. Operators
get the reason from the log.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import structlog

from agentsys.harness.registry import Tier, ToolSpec
from agentsys.services.escalation import EscalationChannel
from agentsys.services.knowledge import KnowledgeBase
from agentsys.services.summaries import ConversationSummarizer

_logger = structlog.get_logger(__name__)

ConnectorOutput = dict[str, Any]
AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]


# ---------------------------------------------------------------------------
# What each tool says when nothing is bound behind it
# ---------------------------------------------------------------------------

_KNOWLEDGE_NOT_CONFIGURED = (
    "No knowledge base is available on this deployment, so nothing could be "
    "looked up. Do NOT answer from memory as though you had consulted a "
    "policy document — say you cannot check it and offer to hand the customer "
    "to a human."
)

_SUMMARY_NOT_CONFIGURED = (
    "No conversation history is available on this deployment, so this "
    "conversation could NOT be summarized. Do not describe what was said "
    "earlier as if you had read it."
)

_ESCALATION_NOT_CONFIGURED = (
    "No escalation channel is configured, so NO human was notified and nobody "
    "is coming. Do not tell the customer that someone will contact them. Say "
    "plainly that you could not reach a human, and give them another way to "
    "get one."
)
"""The only one of the three that must tell the model what to do next.

A failed read degrades into "I cannot check that". A failed escalation is
different: the customer already asked for a person. If the tool returns a bare
error the model can quietly drop it, which lands the customer in exactly the
place the fabricated "notified" did — waiting for someone who was never told.
"""

_ESCALATION_UNCONFIRMED = (
    "The escalation channel did not confirm the notification, so it is NOT "
    "certain a human was reached. Do not promise the customer a callback. Tell "
    "them you could not confirm it and give them another way to reach a person."
)
"""Deliberately not the not-configured text.

`notify` returning normally asserts, per the protocol, that a human WAS
reached; the platform simply cannot identify the escalation. Saying "no
channel is configured" would be false, and re-escalating on that basis would
page the same humans twice. Unconfirmed is its own outcome.
"""

_FAILED_MESSAGES = {
    "knowledge_search_failed": (
        "The knowledge base could not be reached, so nothing was looked up. "
        "Say so plainly — do not answer from memory as if you had consulted it."
    ),
    "summarization_failed": (
        "The conversation history could not be read, so no summary was "
        "produced. Do not describe what was said earlier."
    ),
    "escalation_failed": (
        "The escalation could not be delivered, so NO human was notified. Tell "
        "the customer nobody was reached and give them another way to get a "
        "person."
    ),
}
"""Fixed text, deliberately.

Never interpolate the caught exception. A driver or HTTP client error
stringifies the request plus connection detail, and for a URL-style DSN that
includes the password. It would land in the agent's message history, get
summarized, and be echoed to a customer. The real exception goes to the log.
"""


def _not_configured(message: str, kind: str) -> ConnectorOutput:
    return {"error": message, "error_kind": kind}


def _failed(kind: str) -> ConnectorOutput:
    return {"error": _FAILED_MESSAGES[kind], "error_kind": kind}


def _malformed(result: Any, *, event: str, kind: str) -> ConnectorOutput | None:
    """Return the failure result when *result* is not a dict, else ``None``.

    A port belongs to the deployment, so what it hands back is untrusted input.
    `None` is the case that motivated this: `agent/graph.py` renders a non-dict
    tool output with `str(output)`, so a port returning `None` reached the
    model as the literal string "None" — no `error_kind`, no exception, and
    nothing the model could tell apart from a real answer.

    This is NOT the emptiness check the two read tools deliberately go without.
    An empty `results` list from a real knowledge base is a legitimate answer
    and is relayed as one. A non-dict is the port failing to honour its
    protocol, which is a different fact and the only honest report of it is
    that the tool did not produce an answer.
    """
    if isinstance(result, dict):
        return None
    _logger.error(event, result_type=type(result).__name__)
    return _failed(kind)


# ---------------------------------------------------------------------------
# knowledge_retrieval
# ---------------------------------------------------------------------------

_KNOWLEDGE_DESCRIPTION = (
    "Search the organizational knowledge base. Returns a list of matching "
    "knowledge hits with id, title, and snippet. An empty results list means "
    "the knowledge base holds nothing on the topic — it does not mean you may "
    "answer from your own knowledge instead."
)

_KNOWLEDGE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "q": {
            "type": "string",
            "description": (
                "Natural-language knowledge query, e.g. 'return policy' or "
                "'delivery zones'."
            ),
        }
    },
    "required": ["q"],
}


def build_knowledge_retrieval_connector(
    knowledge_base: KnowledgeBase | None,
) -> AsyncConnector:
    """Build the async `knowledge_retrieval` connector over *knowledge_base*."""

    async def knowledge_retrieval(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        if knowledge_base is None:
            return _not_configured(
                _KNOWLEDGE_NOT_CONFIGURED, "knowledge_not_configured"
            )

        query = str(inputs.get("q") or "").strip()
        if not query:
            # The stub answered an empty query with its whole fixture. Against a
            # real knowledge base that is an unbounded scan, and the result the
            # model gets back is "everything", which it cannot distinguish from
            # "everything relevant".
            return {
                "error": (
                    "No query was given, so nothing was looked up. Ask the "
                    "knowledge base a specific question."
                ),
                "error_kind": "invalid_query",
            }

        try:
            result = await knowledge_base.search(session, query=query)
        except Exception:
            _logger.exception("knowledge.search_failed")
            return _failed("knowledge_search_failed")

        return (
            _malformed(
                result,
                event="knowledge.malformed_result",
                kind="knowledge_search_failed",
            )
            or result
        )

    return knowledge_retrieval


def build_knowledge_retrieval_tool_spec(
    knowledge_base: KnowledgeBase | None,
) -> ToolSpec:
    """Return the `knowledge_retrieval` ToolSpec, unregistered."""
    return ToolSpec(
        name="knowledge_retrieval",
        description=_KNOWLEDGE_DESCRIPTION,
        required_permissions=("read:knowledge_base",),
        input_schema=_KNOWLEDGE_INPUT_SCHEMA,
        connector=build_knowledge_retrieval_connector(knowledge_base),
        tier=Tier.T1,
    )


# ---------------------------------------------------------------------------
# conversation_summarizer
# ---------------------------------------------------------------------------

_SUMMARIZER_DESCRIPTION = (
    "Summarize a conversation session. Returns the session_id, a summary "
    "text, and message_count. Use max_messages to bound how many recent "
    "messages are considered. If this returns an error, you have NOT read the "
    "conversation and must not describe what it contained."
)

_SUMMARIZER_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "session_id": {
            "type": "string",
            "description": "Conversation session identifier",
        },
        "max_messages": {
            "type": "integer",
            "description": (
                "Optional cap on the number of most recent messages to summarize"
            ),
        },
    },
    "required": ["session_id"],
}


def build_conversation_summarizer_connector(
    summarizer: ConversationSummarizer | None,
) -> AsyncConnector:
    """Build the async `conversation_summarizer` connector over *summarizer*."""

    async def conversation_summarizer(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        if summarizer is None:
            return _not_configured(
                _SUMMARY_NOT_CONFIGURED, "summarization_not_configured"
            )

        session_id = str(inputs.get("session_id") or "").strip()
        if not session_id:
            # The stub defaulted to "s-unknown" and summarized its fixture
            # anyway, so a missing id produced a confident summary of a
            # conversation that was never identified.
            return {
                "error": (
                    "No session_id was given, so no conversation was summarized."
                ),
                "error_kind": "invalid_summary_request",
            }

        max_messages = inputs.get("max_messages")
        if not isinstance(max_messages, int) or isinstance(max_messages, bool):
            # Passed through as "no cap" rather than coerced: only the
            # implementation knows what a message is in its store.
            max_messages = None

        try:
            result = await summarizer.summarize(
                session, session_id=session_id, max_messages=max_messages
            )
        except Exception:
            _logger.exception("summary.failed", session_id=session_id)
            return _failed("summarization_failed")

        return (
            _malformed(
                result,
                event="summary.malformed_result",
                kind="summarization_failed",
            )
            or result
        )

    return conversation_summarizer


def build_conversation_summarizer_tool_spec(
    summarizer: ConversationSummarizer | None,
) -> ToolSpec:
    """Return the `conversation_summarizer` ToolSpec, unregistered."""
    return ToolSpec(
        name="conversation_summarizer",
        description=_SUMMARIZER_DESCRIPTION,
        required_permissions=("read:conversation_logs",),
        input_schema=_SUMMARIZER_INPUT_SCHEMA,
        connector=build_conversation_summarizer_connector(summarizer),
        tier=Tier.T1,
    )


# ---------------------------------------------------------------------------
# escalation_notifier
# ---------------------------------------------------------------------------

_ESCALATION_DESCRIPTION = (
    "Notify a human operator that the conversation needs escalation. Requires "
    "a reason and supporting details. A human has been reached only if this "
    "returns an escalation_id — if it returns an error, nobody was notified "
    "and you must tell the customer so."
)

_ESCALATION_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {
            "type": "string",
            "description": "Short reason for the escalation, e.g. 'customer_angry'",
        },
        "details": {
            "type": "string",
            "description": "Supporting context for the human operator",
        },
    },
    "required": ["reason", "details"],
}


def build_escalation_notifier_connector(
    channel: EscalationChannel | None,
) -> AsyncConnector:
    """Build the async `escalation_notifier` connector over *channel*."""

    async def escalation_notifier(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        if channel is None:
            return _not_configured(
                _ESCALATION_NOT_CONFIGURED, "escalation_not_configured"
            )

        reason = str(inputs.get("reason") or "").strip()
        details = str(inputs.get("details") or "").strip()
        if not reason or not details:
            # A human paged with no reason cannot act on it, so this is not a
            # formality: it is the difference between an escalation and a
            # notification nobody can use.
            return {
                "error": (
                    "An escalation needs both a reason and supporting details, "
                    "so nobody was notified. Say what is wrong and what the "
                    "human needs to know, then try again."
                ),
                "error_kind": "invalid_escalation",
            }

        try:
            result = await channel.notify(session, reason=reason, details=details)
        except Exception:
            _logger.exception("escalation.failed", reason=reason)
            return _failed("escalation_failed")

        # The channel belongs to the deployment, so its result is untrusted
        # input. This tool's description promises the model that a human was
        # reached only if an escalation_id comes back; relaying
        # `{"status": "notified"}` with nothing identifying the escalation
        # would make that promise false and put the fabrication back exactly
        # where it was.
        #
        # The two read tools above deliberately get no equivalent check: they
        # have no side effect to confirm, and an empty result from a real
        # knowledge base is a legitimate answer rather than a broken contract.
        escalation_id = (
            str(result.get("escalation_id") or "").strip()
            if isinstance(result, dict)
            else ""
        )
        if not escalation_id:
            _logger.error(
                "escalation.unconfirmed",
                reason=reason,
                result_type=type(result).__name__,
                result_keys=sorted(result) if isinstance(result, dict) else None,
            )
            return {
                "error": _ESCALATION_UNCONFIRMED,
                "error_kind": "escalation_unconfirmed",
            }

        return result

    return escalation_notifier


def build_escalation_notifier_tool_spec(
    channel: EscalationChannel | None,
) -> ToolSpec:
    """Return the `escalation_notifier` ToolSpec, unregistered."""
    return ToolSpec(
        name="escalation_notifier",
        description=_ESCALATION_DESCRIPTION,
        required_permissions=("send:escalation",),
        input_schema=_ESCALATION_INPUT_SCHEMA,
        connector=build_escalation_notifier_connector(channel),
        tier=Tier.T2,
    )
