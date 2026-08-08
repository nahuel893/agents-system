"""Tests for platform-generic connector stubs.

The three platform tools (knowledge_retrieval, conversation_summarizer,
escalation_notifier) are declared by the generic roles under platform/roles/
(data-agent, summary-agent, orchestrator). These deterministic stubs let all
four generic roles boot without real external systems (knowledge base,
conversation store, escalation channel).

Strict TDD: tests written before platform_stubs.py exists.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import pytest

# D-023 added run_report to both registries. It is registered unbound when
# no BI engine is configured, because platform/roles/data-agent names it
# and a tool a manifest names but the registry lacks makes the whole role
# unbuildable through InjectionError.
ALL_PLATFORM_TOOLS = {
    "catalog_search",
    "client_lookup",
    "order_writer",
    "message_sender",
    "session_state",
    "knowledge_retrieval",
    "conversation_summarizer",
    "escalation_notifier",
    "run_report",
}

GENERIC_ROLES = ("sales-agent", "data-agent", "summary-agent", "orchestrator")


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


def _rag_registry() -> Any:
    from agentsys.connectors.rag_connector import build_badie_rag_registry

    return build_badie_rag_registry(_settings(), embedder=SpyEmbedder())


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

    assert "id" in hit
    assert "title" in hit
    assert "snippet" in hit


def test_knowledge_retrieval_empty_query_returns_all_hits() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    result = knowledge_retrieval({"q": ""})

    assert len(result["results"]) >= 1


def test_knowledge_retrieval_is_deterministic() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    first = knowledge_retrieval({"q": "delivery"})
    second = knowledge_retrieval({"q": "delivery"})

    assert first == second


def test_knowledge_retrieval_missing_query_returns_all_hits() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    result = knowledge_retrieval({})

    assert len(result["results"]) == 3


def test_knowledge_retrieval_no_match_returns_first_two_hits() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    result = knowledge_retrieval({"q": "zzz-no-such-topic"})

    assert [hit["id"] for hit in result["results"]] == ["kb-001", "kb-002"]


def test_knowledge_retrieval_query_is_case_insensitive() -> None:
    from agentsys.connectors.platform_stubs import knowledge_retrieval

    lower = knowledge_retrieval({"q": "pricing"})
    upper = knowledge_retrieval({"q": "PRICING"})

    assert upper["results"]
    assert upper == lower


# ---------------------------------------------------------------------------
# conversation_summarizer
# ---------------------------------------------------------------------------

def test_conversation_summarizer_returns_summary_dict() -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    result = conversation_summarizer({"session_id": "s-001"})

    assert result["session_id"] == "s-001"
    assert isinstance(result["summary"], str)
    assert result["summary"]
    assert isinstance(result["message_count"], int)


def test_conversation_summarizer_is_deterministic() -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    first = conversation_summarizer({"session_id": "s-001"})
    second = conversation_summarizer({"session_id": "s-001"})

    assert first == second


def test_conversation_summarizer_max_messages_caps_count() -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    full = conversation_summarizer({"session_id": "s-001"})
    capped = conversation_summarizer({"session_id": "s-001", "max_messages": 1})

    assert capped["message_count"] == 1
    assert capped["message_count"] < full["message_count"]


@pytest.mark.parametrize("bad_max", ["2", 2.5])
def test_conversation_summarizer_non_int_max_messages_ignored(bad_max: Any) -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    full = conversation_summarizer({"session_id": "s-001"})
    result = conversation_summarizer({"session_id": "s-001", "max_messages": bad_max})

    assert result["message_count"] == full["message_count"]


def test_conversation_summarizer_missing_session_id_uses_default() -> None:
    from agentsys.connectors.platform_stubs import conversation_summarizer

    result = conversation_summarizer({})

    assert result["session_id"] == "s-unknown"
    assert result["message_count"] >= 1


# ---------------------------------------------------------------------------
# escalation_notifier
# ---------------------------------------------------------------------------

def test_escalation_notifier_returns_notified_status() -> None:
    from agentsys.connectors.platform_stubs import escalation_notifier

    result = escalation_notifier({"reason": "customer_angry", "details": "Asked for a manager"})

    assert result["status"] == "notified"
    assert re.fullmatch(r"esc-\d{4}", result["escalation_id"])


def test_escalation_notifier_ids_increment() -> None:
    from agentsys.connectors.platform_stubs import escalation_notifier

    first = escalation_notifier({"reason": "r", "details": "d"})
    second = escalation_notifier({"reason": "r", "details": "d"})

    assert first["escalation_id"] != second["escalation_id"]


# ---------------------------------------------------------------------------
# Registry builders — both must contain all 8 tools
# ---------------------------------------------------------------------------

def _assert_platform_tools(registry: Any) -> None:
    assert set(registry.names()) == ALL_PLATFORM_TOOLS
    assert registry.get("knowledge_retrieval").required_permissions == ("read:knowledge_base",)
    assert registry.get("conversation_summarizer").required_permissions == ("read:conversation_logs",)
    assert registry.get("escalation_notifier").required_permissions == ("send:escalation",)


def test_build_badie_registry_contains_all_platform_tools() -> None:
    from agentsys.connectors.stubs import build_badie_registry

    _assert_platform_tools(build_badie_registry())


def test_build_badie_rag_registry_contains_all_platform_tools() -> None:
    _assert_platform_tools(_rag_registry())


def test_platform_tool_input_schemas_have_required_lists() -> None:
    from agentsys.connectors.stubs import build_badie_registry

    registry = build_badie_registry()

    assert "q" in registry.get("knowledge_retrieval").input_schema["required"]
    assert "session_id" in registry.get("conversation_summarizer").input_schema["required"]
    assert set(registry.get("escalation_notifier").input_schema["required"]) == {"reason", "details"}


# ---------------------------------------------------------------------------
# Injector-level — every generic role resolves with zero denials
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role_type", GENERIC_ROLES)
def test_generic_role_resolves_full_tool_surface(role_type: str) -> None:
    from agentsys.connectors.stubs import build_badie_registry
    from agentsys.harness import loader
    from agentsys.harness.injector import resolve_tool_surface

    definition = loader.resolve(role_type, client=None)
    registry = build_badie_registry()

    # Mirrors main.py: the role's own resolved permissions are the grants.
    result = resolve_tool_surface(
        definition, registry, granted_permissions=definition.permissions
    )

    assert result.denied == ()
    assert {t.name for t in result.granted} == set(definition.tools)
