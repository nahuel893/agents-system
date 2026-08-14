"""Tests for platform-generic connector stubs.

The three platform tools (knowledge_retrieval, conversation_summarizer,
escalation_notifier) are declared by the generic roles under platform/roles/
(data-agent, summary-agent, orchestrator). These deterministic stubs let all
generic roles boot without real external systems (knowledge base, conversation
store, escalation channel).

Assertion policy in this file: expected values are written out as literals, not
derived from the object under test. A test that compares a pure function to
itself ("call it twice, assert equal") cannot fail for any implementation and
is not written here.

Tests whose name ends in ``_documented_gap`` pin behaviour that is currently
wrong or hazardous and cannot be corrected without changing production code.
They exist so the behaviour is visible and so a later fix is a deliberate,
reviewed deletion rather than a silent drift.

Strict TDD: tests written before platform_stubs.py exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from platform_role_contract import (
    EXPECTED_ROLE_TOOLS,
    PINNED_ROLES,
    discover_platform_roles,
)

ALL_EIGHT_TOOLS = {
    "catalog_search",
    "client_lookup",
    "order_writer",
    "message_sender",
    "session_state",
    "knowledge_retrieval",
    "conversation_summarizer",
    "escalation_notifier",
}

#: The knowledge base fixture, written out independently of the module under
#: test. Also the corruption canary: knowledge_retrieval hands back the shared
#: module-level list by reference, so any caller that mutates a result would
#: rewrite the fixture for the whole process — this literal notices.
CANONICAL_HITS = [
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
        "snippet": (
            "Claims are accepted within 48 hours of delivery with the "
            "original invoice."
        ),
    },
]

EXPECTED_SUMMARY = (
    "Customer asked about product availability and confirmed a purchase of "
    "two units."
)

#: Tools both registry builders must wire identically. catalog_search is
#: excluded on purpose: the RAG registry swaps in a semantic-search connector
#: with its own schema.
SHARED_TOOLS = tuple(sorted(ALL_EIGHT_TOOLS - {"catalog_search"}))


@dataclass
class SpyEmbedder:
    """Embedder that records calls but never actually embeds."""

    calls: list[list[str]] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=lambda: [[0.1, 0.2, 0.3]])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


def _settings() -> Any:
    from agentsys.config import Settings

    return Settings(_env_file=None)


def _stub_registry() -> Any:
    from agentsys.connectors.stubs import build_badie_registry

    return build_badie_registry()


def _rag_registry() -> Any:
    from agentsys.connectors.rag_connector import build_badie_rag_registry

    return build_badie_rag_registry(_settings(), embedder=SpyEmbedder())


#: Both registry builders, so every registry assertion also runs against the
#: one production actually uses (build_badie_rag_registry — main.py,
#: scripts/chat.py, scripts/smoke_chat.py), not only the stub registry.
REGISTRY_BUILDERS = {"stub": _stub_registry, "rag": _rag_registry}


def _escalation_number(escalation_id: str) -> int:
    match = re.fullmatch(r"esc-(\d{4,})", escalation_id)
    assert match is not None, f"unexpected escalation_id format: {escalation_id!r}"
    return int(match.group(1))


def _without_descriptions(node: Any) -> Any:
    """Drop every ``description`` key so schema parity ignores prose drift."""
    if isinstance(node, dict):
        return {
            key: _without_descriptions(value)
            for key, value in node.items()
            if key != "description"
        }
    if isinstance(node, list):
        return [_without_descriptions(value) for value in node]
    return node


# ---------------------------------------------------------------------------
# knowledge_retrieval
# ---------------------------------------------------------------------------

def test_knowledge_retrieval_returns_results_list() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    result = knowledge_retrieval({"q": "pricing"})

    assert "results" in result
    assert isinstance(result["results"], list)
    assert len(result["results"]) >= 1


def test_knowledge_retrieval_hit_shape() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    hit = knowledge_retrieval({"q": "pricing"})["results"][0]

    assert set(hit) == {"id", "title", "snippet"}


def test_knowledge_retrieval_empty_query_returns_every_canonical_hit() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    result = knowledge_retrieval({"q": ""})

    assert result["results"] == CANONICAL_HITS


def test_knowledge_retrieval_missing_query_returns_every_canonical_hit() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    result = knowledge_retrieval({})

    assert result["results"] == CANONICAL_HITS


def test_knowledge_retrieval_matches_title_and_snippet() -> None:
    """'delivery' hits kb-002 by title AND kb-003 by snippet — both, in order."""
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    result = knowledge_retrieval({"q": "delivery"})

    assert result["results"] == [CANONICAL_HITS[1], CANONICAL_HITS[2]]


def test_knowledge_retrieval_no_match_returns_first_two_hits() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    result = knowledge_retrieval({"q": "zzz-no-such-topic"})

    assert result["results"] == CANONICAL_HITS[:2]


def test_knowledge_retrieval_query_is_case_insensitive() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    upper = knowledge_retrieval({"q": "PRICING"})

    assert upper["results"] == [CANONICAL_HITS[0]]


def test_knowledge_retrieval_results_alias_the_shared_fixture_documented_gap() -> None:
    """knowledge_retrieval hands back the module-level list BY REFERENCE.

    Latent today (``_execute_tools`` only json.dumps the output), but any
    post-processing added between connector and ToolMessage — redaction,
    scoring, annotation — would rewrite the knowledge base for the whole
    process lifetime. Closing this needs a defensive copy in production code;
    this test pins the hazard and goes red the moment that copy lands.
    """
    from agentsys.connectors import platform_stubs

    results = platform_stubs.knowledge_retrieval({"q": ""})["results"]

    assert results is platform_stubs._KNOWLEDGE_HITS

    original = results[0]["snippet"]
    try:
        results[0]["snippet"] = "MUTATED"
        assert (
            platform_stubs.knowledge_retrieval({})["results"][0]["snippet"]
            == "MUTATED"
        )
    finally:
        results[0]["snippet"] = original

    assert platform_stubs.knowledge_retrieval({})["results"] == CANONICAL_HITS


# ---------------------------------------------------------------------------
# conversation_summarizer
# ---------------------------------------------------------------------------

def test_conversation_summarizer_returns_pinned_summary_dict() -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    result = conversation_summarizer({"session_id": "s-001"})

    assert result == {
        "session_id": "s-001",
        "summary": EXPECTED_SUMMARY,
        "message_count": 3,
    }


@pytest.mark.parametrize("session_id", ["s-001", "s-002"])
def test_conversation_summarizer_echoes_the_requested_session_id(
    session_id: str,
) -> None:
    """The echo is what makes a summary attributable to a conversation."""
    from agentsys.connectors.platform_stubs import conversation_summarizer

    result = conversation_summarizer({"session_id": session_id})

    assert result["session_id"] == session_id


def test_conversation_summarizer_max_messages_caps_count() -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    capped = conversation_summarizer({"session_id": "s-001", "max_messages": 1})

    assert capped == {
        "session_id": "s-001",
        "summary": EXPECTED_SUMMARY,
        "message_count": 1,
    }


@pytest.mark.parametrize("bad_max", ["2", 2.5, None, []])
def test_conversation_summarizer_non_int_max_messages_ignored(bad_max: Any) -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    result = conversation_summarizer({"session_id": "s-001", "max_messages": bad_max})

    assert result["message_count"] == 3


@pytest.mark.parametrize(
    ("max_messages", "message_count"),
    [(True, 1), (False, 0), (0, 0), (-1, 2)],
)
def test_conversation_summarizer_accepts_bool_zero_and_negative_documented_gap(
    max_messages: Any, message_count: int
) -> None:
    """``isinstance(True, int)`` is True, and slicing accepts 0 and -1.

    So ``max_messages: true`` — a legal JSON value a model can emit — caps the
    transcript at 1 instead of being ignored like ``"2"`` and ``2.5``;
    ``max_messages: -1``, a common "no limit" idiom, silently drops the last
    message; ``max_messages: 0`` reports message_count 0 while ``summary``
    still asserts a completed two-unit purchase. Rejecting these needs a
    production guard; this pins what happens today so the fix is visible.
    """
    from agentsys.connectors.platform_stubs import conversation_summarizer

    result = conversation_summarizer(
        {"session_id": "s-001", "max_messages": max_messages}
    )

    assert result["message_count"] == message_count


def test_conversation_summarizer_missing_session_id_uses_default() -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    result = conversation_summarizer({})

    assert result == {
        "session_id": "s-unknown",
        "summary": EXPECTED_SUMMARY,
        "message_count": 3,
    }


# ---------------------------------------------------------------------------
# escalation_notifier
# ---------------------------------------------------------------------------

def test_escalation_notifier_returns_notified_status() -> None:
    from agentsys.connectors.platform_stubs import escalation_notifier

    result = escalation_notifier(
        {"reason": "customer_angry", "details": "Asked for a manager"}
    )

    assert set(result) == {"status", "escalation_id", "reason"}
    assert result["reason"] == "customer_angry"
    assert result["status"] == "notified"
    assert _escalation_number(result["escalation_id"]) >= 1


def test_escalation_notifier_ids_are_unique_and_increase_by_one() -> None:
    """Zero-padded to at LEAST 4 digits — not exactly 4.

    ``f"esc-{n:04d}"`` over a process-global counter emits ``esc-10000`` on the
    10000th call, which an ``esc-\\d{4}`` matcher rejects. Pinning exactly four
    digits only passes because the test process never gets there.
    """
    from agentsys.connectors.platform_stubs import escalation_notifier

    ids = [
        escalation_notifier({"reason": "r", "details": "d"})["escalation_id"]
        for _ in range(5)
    ]
    numbers = [_escalation_number(value) for value in ids]

    assert len(set(ids)) == 5
    assert numbers == list(range(numbers[0], numbers[0] + 5))


def test_escalation_notifier_echoes_the_reason_it_was_given() -> None:
    """The ToolSpec declares ``required: [reason, details]``, so the body must
    read at least one of them.

    Every other stub echoes an identifying input (message_sender->to,
    client_lookup->phone, session_state->session_id,
    conversation_summarizer->session_id). Without that, an operator queue
    built on this output cannot tell one escalation from another, and a later
    real implementation that dropped the reason on the floor would still pass.

    The `empty` leg is the point: a missing reason must surface as None rather
    than vanish, so the key is always present and an absent reason is visible
    instead of indistinguishable from a stub that never looked.
    """
    from agentsys.connectors.platform_stubs import escalation_notifier

    populated = escalation_notifier(
        {"reason": "customer_angry", "details": "Asked for a manager"}
    )
    empty = escalation_notifier({})

    assert set(populated) == set(empty) == {"status", "escalation_id", "reason"}
    assert populated["reason"] == "customer_angry"
    assert empty["reason"] is None
    assert populated["status"] == empty["status"]
    assert _escalation_number(empty["escalation_id"]) == (
        _escalation_number(populated["escalation_id"]) + 1
    )


# ---------------------------------------------------------------------------
# Registry builders — both must contain all 8 tools, wired identically
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("builder_name", sorted(REGISTRY_BUILDERS))
def test_registry_contains_all_eight_tools(builder_name: str) -> None:
    registry = REGISTRY_BUILDERS[builder_name]()

    assert set(registry.names()) == ALL_EIGHT_TOOLS
    assert registry.get("knowledge_retrieval").required_permissions == (
        "read:knowledge_base",
    )
    assert registry.get("conversation_summarizer").required_permissions == (
        "read:conversation_logs",
    )
    assert registry.get("escalation_notifier").required_permissions == (
        "send:escalation",
    )


@pytest.mark.parametrize("builder_name", sorted(REGISTRY_BUILDERS))
def test_platform_tool_input_schemas_have_required_lists(builder_name: str) -> None:
    registry = REGISTRY_BUILDERS[builder_name]()

    assert registry.get("knowledge_retrieval").input_schema["required"] == ["q"]
    assert registry.get("conversation_summarizer").input_schema["required"] == [
        "session_id"
    ]
    assert set(registry.get("escalation_notifier").input_schema["required"]) == {
        "reason",
        "details",
    }


@pytest.mark.parametrize("tool_name", SHARED_TOOLS)
def test_both_registries_wire_shared_tools_identically(tool_name: str) -> None:
    """The two builders hand-duplicate their specs, so they can drift.

    Production uses build_badie_rag_registry (main.py, scripts/chat.py,
    scripts/smoke_chat.py); most tests reach for build_badie_registry. Drop
    ``session_id`` from the RAG copy of conversation_summarizer's ``required``
    and every production summary silently becomes ``session_id="s-unknown"``.
    Schema prose is compared with descriptions stripped: they already differ by
    a trailing period on knowledge_retrieval's ``q``.
    """
    stub_spec = _stub_registry().get(tool_name)
    rag_spec = _rag_registry().get(tool_name)

    assert stub_spec.required_permissions == rag_spec.required_permissions
    assert stub_spec.connector is rag_spec.connector
    assert _without_descriptions(stub_spec.input_schema) == _without_descriptions(
        rag_spec.input_schema
    )


# ---------------------------------------------------------------------------
# Injector-level — every platform role resolves against a literal expectation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role_type", PINNED_ROLES)
def test_platform_role_resolves_its_pinned_tool_surface(role_type: str) -> None:
    """Expected surface is a literal, NOT ``set(definition.tools)``.

    Grading the resolved surface against the manifest that produced it means
    deleting a tool from a manifest keeps the assertion green (2 == 2).
    """
    from agentsys.harness import loader
    from agentsys.harness.injector import resolve_tool_surface

    definition = loader.resolve(role_type, client=None)
    registry = _stub_registry()

    # Mirrors main.py: the role's own resolved permissions are the grants.
    result = resolve_tool_surface(
        definition, registry, granted_permissions=definition.permissions
    )

    assert result.denied == ()
    assert set(definition.tools) == EXPECTED_ROLE_TOOLS[role_type]
    assert {t.name for t in result.granted} == EXPECTED_ROLE_TOOLS[role_type]


@pytest.mark.parametrize("role_type", discover_platform_roles())
@pytest.mark.parametrize("builder_name", sorted(REGISTRY_BUILDERS))
def test_every_tool_declared_on_disk_exists_in_both_registries(
    builder_name: str, role_type: str
) -> None:
    """The role list comes from disk, so role #5 is covered without a tuple edit.

    A manifest naming a tool no registry holds makes the injector raise
    ``InjectionError`` and the role unbootable — the failure class this branch
    exists to eliminate.
    """
    from agentsys.harness import loader

    definition = loader.resolve(role_type, client=None)
    registry_names = set(REGISTRY_BUILDERS[builder_name]().names())

    missing = sorted(set(definition.tools) - registry_names)
    assert missing == [], (
        f"role '{role_type}' declares {missing}, absent from the "
        f"{builder_name} registry — the role cannot boot"
    )
