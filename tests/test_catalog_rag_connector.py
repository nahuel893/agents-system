"""Unit tests for the RAG catalog connector (D-010).

Strict TDD: these tests are written BEFORE the production module exists.
All external DB calls are monkeypatched — no real Postgres required.

asyncio_mode = "auto" (set in pyproject.toml) — async tests need NO marker.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from agentsys.config import Settings
from agentsys.services import rag


# ---------------------------------------------------------------------------
# Shared fake stubs
# ---------------------------------------------------------------------------

@dataclass
class SpyEmbedder:
    """Embedder that records calls but never actually embeds."""

    calls: list[list[str]] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=lambda: [[0.1, 0.2, 0.3]])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


def _settings(**kwargs: Any) -> Settings:
    return Settings(
        _env_file=None,
        rag_threshold_direct=0.92,
        rag_threshold_ambiguous=0.82,
        rag_top_k=3,
        rag_keyword_top_k=3,
        **kwargs,
    )


def _make_registry(embedder: Any = None, settings: Settings | None = None) -> Any:
    from agentsys.connectors.rag_connector import build_badie_rag_registry

    s = settings or _settings()
    if embedder is not None:
        return build_badie_rag_registry(s, embedder=embedder)
    return build_badie_rag_registry(s, embedder=SpyEmbedder())


# ---------------------------------------------------------------------------
# Test 1: connector is a coroutine function
# ---------------------------------------------------------------------------

def test_connector_is_async_coroutine_function() -> None:
    """The connector must be a true async def so D-009 dispatch routes it correctly."""
    from agentsys.connectors.rag_connector import build_catalog_rag_connector

    embedder = SpyEmbedder()
    connector = build_catalog_rag_connector(embedder, _settings())
    assert asyncio.iscoroutinefunction(connector)


# ---------------------------------------------------------------------------
# Test 2: direct match maps to results + classification
# ---------------------------------------------------------------------------

async def test_direct_match_maps_to_results_and_classification(
    monkeypatch: Any,
) -> None:
    from agentsys.services.catalog import VectorSearchCandidate
    from agentsys.connectors.rag_connector import build_catalog_rag_connector

    async def fake_search_vector(session: Any, *, embedding: Any, limit: int, ef_search: int) -> list[Any]:
        return [
            VectorSearchCandidate("SKU-A1", "Aceite de girasol 900ml", 0.04),  # similarity 0.96
        ]

    monkeypatch.setattr(rag.catalog, "search_vector", fake_search_vector)

    embedder = SpyEmbedder()
    connector = build_catalog_rag_connector(embedder, _settings())
    result = await connector({"q": "aceite de girasol"}, session=object())

    assert result["classification"] == "direct"
    assert len(result["results"]) == 1
    assert result["results"][0]["sku"] == "SKU-A1"
    assert result["results"][0]["description"] == "Aceite de girasol 900ml"
    assert isinstance(result["results"][0]["similarity"], float)


# ---------------------------------------------------------------------------
# Test 3: ambiguous match mapping
# ---------------------------------------------------------------------------

async def test_ambiguous_match_mapping(monkeypatch: Any) -> None:
    from agentsys.services.catalog import VectorSearchCandidate
    from agentsys.connectors.rag_connector import build_catalog_rag_connector

    async def fake_search_vector(session: Any, *, embedding: Any, limit: int, ef_search: int) -> list[Any]:
        return [
            VectorSearchCandidate("SKU-B1", "Coca-Cola 2.25L", 0.15),  # similarity 0.85
            VectorSearchCandidate("SKU-B2", "Coca-Cola Zero 2.25L", 0.17),  # similarity 0.83
        ]

    monkeypatch.setattr(rag.catalog, "search_vector", fake_search_vector)

    embedder = SpyEmbedder()
    connector = build_catalog_rag_connector(embedder, _settings())
    result = await connector({"q": "coca cola"}, session=object())

    assert result["classification"] == "ambiguous"
    assert len(result["results"]) == 2
    assert all(isinstance(r["similarity"], float) for r in result["results"])


# ---------------------------------------------------------------------------
# Test 4: no match returns empty results
# ---------------------------------------------------------------------------

async def test_no_match_returns_empty_results(monkeypatch: Any) -> None:
    from agentsys.services.catalog import VectorSearchCandidate
    from agentsys.connectors.rag_connector import build_catalog_rag_connector

    async def fake_search_vector(session: Any, *, embedding: Any, limit: int, ef_search: int) -> list[Any]:
        return [
            VectorSearchCandidate("SKU-C1", "Agua mineral", 0.25),  # similarity 0.75 — below threshold
        ]

    monkeypatch.setattr(rag.catalog, "search_vector", fake_search_vector)

    embedder = SpyEmbedder()
    connector = build_catalog_rag_connector(embedder, _settings())
    result = await connector({"q": "xyzzy nonsense"}, session=object())

    assert result == {"results": [], "classification": "no_match"}


# ---------------------------------------------------------------------------
# Test 5: keyword fallback similarity is None (not coerced to 0.0)
# ---------------------------------------------------------------------------

async def test_keyword_fallback_similarity_is_null(monkeypatch: Any) -> None:
    from agentsys.services.catalog import KeywordSearchCandidate
    from agentsys.connectors.rag_connector import build_catalog_rag_connector

    # Embedder returns empty vector → triggers keyword fallback
    embedder = SpyEmbedder(vectors=[])

    async def fake_search_keywords(session: Any, *, query: str, limit: int) -> list[Any]:
        return [KeywordSearchCandidate("SKU-K1", "Quilmes 1L")]

    monkeypatch.setattr(rag.catalog, "search_keywords", fake_search_keywords)

    connector = build_catalog_rag_connector(embedder, _settings())
    result = await connector({"q": "quilmes"}, session=object())

    assert len(result["results"]) == 1
    assert result["results"][0]["similarity"] is None
    assert result["classification"] == "ambiguous"


# ---------------------------------------------------------------------------
# Test 6: empty query short-circuits without calling embedder
# ---------------------------------------------------------------------------

async def test_empty_q_short_circuits_without_embedding(monkeypatch: Any) -> None:
    from agentsys.connectors.rag_connector import build_catalog_rag_connector

    spy = SpyEmbedder()
    search_vector_called = []

    async def fake_search_vector(session: Any, **kwargs: Any) -> list[Any]:
        search_vector_called.append(True)
        return []

    monkeypatch.setattr(rag.catalog, "search_vector", fake_search_vector)

    connector = build_catalog_rag_connector(spy, _settings())

    # Test empty string
    result = await connector({"q": ""}, session=object())
    assert result == {"results": [], "classification": "no_match"}
    assert spy.calls == []
    assert search_vector_called == []

    # Test whitespace-only string
    result2 = await connector({"q": "   "}, session=object())
    assert result2 == {"results": [], "classification": "no_match"}
    assert spy.calls == []


# ---------------------------------------------------------------------------
# Test 7: embedder built once when not injected
# ---------------------------------------------------------------------------

def test_embedder_built_once_in_registry(monkeypatch: Any) -> None:
    from agentsys.connectors import rag_connector

    provider_calls: list[Any] = []

    def counting_factory(settings: Any) -> SpyEmbedder:
        provider_calls.append(settings)
        return SpyEmbedder()

    monkeypatch.setattr(rag_connector, "get_embedding_provider", counting_factory)

    s = _settings()

    # Without embedder injection — factory should be called once
    rag_connector.build_badie_rag_registry(s)
    assert len(provider_calls) == 1

    # Reset and test with injected embedder — factory should NOT be called
    provider_calls.clear()
    rag_connector.build_badie_rag_registry(s, embedder=SpyEmbedder())
    assert len(provider_calls) == 0


# ---------------------------------------------------------------------------
# Test 8: ToolSpec description has no "price" or "stock"
# ---------------------------------------------------------------------------

def test_catalog_toolspec_description_has_no_price_or_stock() -> None:
    registry = _make_registry()
    desc = registry.get("catalog_search").description.lower()

    assert "price" not in desc
    assert "precio" not in desc
    assert "stock" not in desc


# ---------------------------------------------------------------------------
# Test 9: registry has exactly 5 tools; catalog is async, others sync
# ---------------------------------------------------------------------------

def test_registry_has_five_tools_with_async_catalog() -> None:
    registry = _make_registry()

    assert set(registry.names()) == {
        "catalog_search",
        "client_lookup",
        "order_writer",
        "message_sender",
        "session_state",
    }

    catalog_spec = registry.get("catalog_search")
    assert asyncio.iscoroutinefunction(catalog_spec.connector)

    for name in ("client_lookup", "order_writer", "message_sender", "session_state"):
        spec = registry.get(name)
        assert not asyncio.iscoroutinefunction(spec.connector), (
            f"{name} should be sync but iscoroutinefunction returned True"
        )


# ---------------------------------------------------------------------------
# Test 10: build_runtime wires session_provider correctly
# ---------------------------------------------------------------------------

def _full_sales_registry() -> Any:
    """Registry with all 5 BADIE sales-agent tools (required by sales-agent manifest)."""
    from agentsys.harness.registry import ToolRegistry, ToolSpec

    reg = ToolRegistry()
    dummy = lambda inputs: {}  # noqa: E731
    reg.register(ToolSpec(name="catalog_search", required_permissions=("read:catalog",), connector=dummy))
    reg.register(ToolSpec(name="client_lookup", required_permissions=("read:client_registry",), connector=dummy))
    reg.register(ToolSpec(name="order_writer", required_permissions=("write:orders", "write:order_items"), connector=dummy))
    reg.register(ToolSpec(name="message_sender", required_permissions=("send:message",), connector=dummy))
    reg.register(ToolSpec(name="session_state", required_permissions=(), connector=dummy))
    return reg


def test_build_runtime_wires_session_provider() -> None:
    from agentsys.harness.factory import build_runtime

    reg = _full_sales_registry()
    sentinel = object()

    # With session_provider — should be wired through
    runtime = build_runtime(
        "sales-agent",
        reg,
        ["read:catalog", "read:client_registry", "write:orders", "write:order_items", "send:message"],
        client="badie",
        session_provider=sentinel,
    )
    assert runtime.session_provider is sentinel

    # Without session_provider — should default to None
    runtime2 = build_runtime(
        "sales-agent",
        reg,
        ["read:catalog"],
        client="badie",
    )
    assert runtime2.session_provider is None
