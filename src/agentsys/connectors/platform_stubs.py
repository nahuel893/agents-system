"""Platform-generic connector stubs.

Deterministic fake connectors for the three platform tools declared by the
generic roles under ``platform/roles/`` (data-agent, summary-agent,
orchestrator). They let every generic role boot end-to-end without real
external systems (knowledge base, conversation store, escalation channel).

Each connector follows the same signature: dict[str, Any] -> dict[str, Any].
"""
from __future__ import annotations

import itertools
from typing import Any

_escalation_counter = itertools.count(1)

_KNOWLEDGE_HITS: list[dict[str, Any]] = [
    {
        "id": "kb-001",
        "title": "Wholesale pricing policy",
        "snippet": "Volume discounts apply from 10 units per SKU.",
    },
    {
        "id": "kb-002",
        "title": "Delivery coverage zones",
        "snippet": "Deliveries cover the metropolitan area on business days.",
    },
    {
        "id": "kb-003",
        "title": "Returns and claims procedure",
        "snippet": "Claims are accepted within 48 hours of delivery with the original invoice.",
    },
]

_FAKE_TRANSCRIPT: list[dict[str, str]] = [
    {"role": "customer", "content": "Hi, do you have sugar in stock?"},
    {"role": "agent", "content": "Yes, we have 1kg bags available."},
    {"role": "customer", "content": "Great, I will take two."},
]


def knowledge_retrieval(inputs: dict[str, Any]) -> dict[str, Any]:
    q = (inputs.get("q") or "").lower()
    if not q:
        return {"results": _KNOWLEDGE_HITS}
    matches = [
        hit
        for hit in _KNOWLEDGE_HITS
        if q in hit["title"].lower() or q in hit["snippet"].lower()
    ]
    return {"results": matches if matches else _KNOWLEDGE_HITS[:2]}


def conversation_summarizer(inputs: dict[str, Any]) -> dict[str, Any]:
    session_id = str(inputs.get("session_id", "s-unknown"))
    max_messages = inputs.get("max_messages")
    messages = (
        _FAKE_TRANSCRIPT[:max_messages]
        if isinstance(max_messages, int)
        else _FAKE_TRANSCRIPT
    )
    return {
        "session_id": session_id,
        "summary": (
            "Customer asked about product availability and confirmed a "
            "purchase of two units."
        ),
        "message_count": len(messages),
    }


def escalation_notifier(inputs: dict[str, Any]) -> dict[str, Any]:
    escalation_id = f"esc-{next(_escalation_counter):04d}"
    return {"status": "notified", "escalation_id": escalation_id}
