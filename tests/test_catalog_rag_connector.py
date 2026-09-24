# type: ignore
# pyright: reportMissingImports=false, reportCallIssue=false, reportArgumentType=false
"""Unit tests for the RAG catalog connector (D-010).

Strict TDD: these tests are written BEFORE the production module exists.
All external DB calls are monkeypatched — no real Postgres required.

asyncio_mode = "auto" (set in pyproject.toml) — async tests need NO marker.
"""

from __future__ import annotations

import asyncio
import pathlib
from dataclasses import dataclass, field
from typing import Any

from agents_system.config import Settings

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_CLIENT_A_DEPLOYMENTS = (
    _REPO_ROOT / "tests" / "fixtures" / "agents" / "overrides" / "deployments"
)


def _client_a_roots() -> Any:
    from agents_system.harness.loader import RootConfig

    return RootConfig(
        platform_root=_REPO_ROOT / "platform",
        deployments_root=_CLIENT_A_DEPLOYMENTS,
    )


@dataclass
class StubCatalogSource:
    """A `rag.CatalogSource` wrapping loose fakes — no schema, no database."""

    search_vector_fn: Any = None
    search_keywords_fn: Any = None

    async def search_vector(
        self, session: Any, *, embedding: list[float], limit: int, ef_search: int
    ) -> list[Any]:
        if self.search_vector_fn is None:
            return []
        return await self.search_vector_fn(
            session, embedding=embedding, limit=limit, ef_search=ef_search
        )

    async def search_keywords(
        self, session: Any, *, query: str, limit: int
    ) -> list[Any]:
        if self.search_keywords_fn is None:
            return []
        return await self.search_keywords_fn(session, query=query, limit=limit)


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


# ---------------------------------------------------------------------------
# Test 1: connector is a coroutine function
# ---------------------------------------------------------------------------


def test_connector_is_async_coroutine_function() -> None:
    """The connector must be a true async def so D-009 dispatch routes it correctly."""
    source = StubCatalogSource()
    from agents_system.connectors.rag_connector import build_catalog_rag_connector

    embedder = SpyEmbedder()
    connector = build_catalog_rag_connector(embedder, _settings(), source)
    assert asyncio.iscoroutinefunction(connector)


# ---------------------------------------------------------------------------
# Test 2: direct match maps to results + classification
# ---------------------------------------------------------------------------


async def test_direct_match_maps_to_results_and_classification() -> None:
    from agents_system.services.rag import VectorSearchCandidate
    from agents_system.connectors.rag_connector import build_catalog_rag_connector

    async def fake_search_vector(
        session: Any, *, embedding: Any, limit: int, ef_search: int
    ) -> list[Any]:
        return [
            VectorSearchCandidate(
                "SKU-A1", "Aceite de girasol 900ml", 0.04
            ),  # similarity 0.96
        ]

    source = StubCatalogSource(search_vector_fn=fake_search_vector)

    embedder = SpyEmbedder()
    connector = build_catalog_rag_connector(embedder, _settings(), source)
    result = await connector({"q": "aceite de girasol"}, session=object())

    assert result["classification"] == "direct"
    assert len(result["results"]) == 1
    assert result["results"][0]["sku"] == "SKU-A1"
    assert result["results"][0]["description"] == "Aceite de girasol 900ml"
    assert isinstance(result["results"][0]["similarity"], float)


# ---------------------------------------------------------------------------
# Test 3: ambiguous match mapping
# ---------------------------------------------------------------------------


async def test_ambiguous_match_mapping() -> None:
    from agents_system.services.rag import VectorSearchCandidate
    from agents_system.connectors.rag_connector import build_catalog_rag_connector

    async def fake_search_vector(
        session: Any, *, embedding: Any, limit: int, ef_search: int
    ) -> list[Any]:
        return [
            VectorSearchCandidate("SKU-B1", "Coca-Cola 2.25L", 0.15),  # similarity 0.85
            VectorSearchCandidate(
                "SKU-B2", "Coca-Cola Zero 2.25L", 0.17
            ),  # similarity 0.83
        ]

    source = StubCatalogSource(search_vector_fn=fake_search_vector)

    embedder = SpyEmbedder()
    connector = build_catalog_rag_connector(embedder, _settings(), source)
    result = await connector({"q": "coca cola"}, session=object())

    assert result["classification"] == "ambiguous"
    assert len(result["results"]) == 2
    assert all(isinstance(r["similarity"], float) for r in result["results"])


# ---------------------------------------------------------------------------
# Test 4: no match returns empty results
# ---------------------------------------------------------------------------


async def test_no_match_returns_empty_results() -> None:
    """A vector-path no_match, distinguished from a fallback-path one.

    The migration cost this test its discriminating power and the assertion
    text hid it. Under monkeypatch only `search_vector` was faked, so the
    real `search_keywords` stayed in place and raised on the `object()`
    session — an accidental fall-through to the keyword path was caught by
    that crash. `StubCatalogSource.search_keywords` returns [] silently,
    which produces exactly the asserted result, so the two paths became
    indistinguishable. Asserting the fallback was never consulted restores
    the distinction the section header claims to cover.
    """
    from agents_system.services.rag import VectorSearchCandidate
    from agents_system.connectors.rag_connector import build_catalog_rag_connector

    keyword_calls: list[str] = []

    async def fake_search_keywords(
        session: Any, *, query: str, limit: int
    ) -> list[Any]:
        keyword_calls.append(query)
        return []

    async def fake_search_vector(
        session: Any, *, embedding: Any, limit: int, ef_search: int
    ) -> list[Any]:
        return [
            VectorSearchCandidate(
                "SKU-C1", "Agua mineral", 0.25
            ),  # similarity 0.75 — below threshold
        ]

    source = StubCatalogSource(
        search_vector_fn=fake_search_vector, search_keywords_fn=fake_search_keywords
    )

    embedder = SpyEmbedder()
    connector = build_catalog_rag_connector(embedder, _settings(), source)
    result = await connector({"q": "xyzzy nonsense"}, session=object())

    assert result == {"results": [], "classification": "no_match"}
    assert keyword_calls == [], (
        "the vector path answered no_match; the keyword fallback must not "
        "have been consulted at all"
    )


# ---------------------------------------------------------------------------
# Test 5: keyword fallback similarity is None (not coerced to 0.0)
# ---------------------------------------------------------------------------


async def test_keyword_fallback_similarity_is_null() -> None:
    from agents_system.services.rag import KeywordSearchCandidate
    from agents_system.connectors.rag_connector import build_catalog_rag_connector

    # Embedder returns empty vector → triggers keyword fallback
    embedder = SpyEmbedder(vectors=[])

    async def fake_search_keywords(
        session: Any, *, query: str, limit: int
    ) -> list[Any]:
        return [KeywordSearchCandidate("SKU-K1", "BrandA 1L")]

    source = StubCatalogSource(search_keywords_fn=fake_search_keywords)

    connector = build_catalog_rag_connector(embedder, _settings(), source)
    result = await connector({"q": "branda"}, session=object())

    assert len(result["results"]) == 1
    assert result["results"][0]["similarity"] is None
    assert result["classification"] == "ambiguous"


# ---------------------------------------------------------------------------
# Test 6: empty query short-circuits without calling embedder
# ---------------------------------------------------------------------------


async def test_empty_q_short_circuits_without_embedding() -> None:
    from agents_system.connectors.rag_connector import build_catalog_rag_connector

    spy = SpyEmbedder()
    search_vector_called = []

    async def fake_search_vector(session: Any, **kwargs: Any) -> list[Any]:
        search_vector_called.append(True)
        return []

    source = StubCatalogSource(search_vector_fn=fake_search_vector)

    connector = build_catalog_rag_connector(spy, _settings(), source)

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
# Test 7: build_runtime wires session_provider correctly
# ---------------------------------------------------------------------------


def _full_sales_registry() -> Any:
    """Registry with all five generic deployment sales-agent tools."""
    from agents_system.harness.registry import Tier, ToolRegistry, ToolSpec

    reg = ToolRegistry()
    dummy = lambda inputs: {}  # noqa: E731
    reg.register(
        ToolSpec(
            name="catalog_search",
            required_permissions=("read:catalog",),
            connector=dummy,
            tier=Tier.T1,
        )
    )
    reg.register(
        ToolSpec(
            name="client_lookup",
            required_permissions=("read:client_registry",),
            connector=dummy,
            tier=Tier.T1,
        )
    )
    reg.register(
        ToolSpec(
            name="order_writer",
            required_permissions=("write:orders", "write:order_items"),
            connector=dummy,
            tier=Tier.T2,
        )
    )
    reg.register(
        ToolSpec(
            name="message_sender",
            required_permissions=("send:message",),
            connector=dummy,
            tier=Tier.T2,
        )
    )
    reg.register(
        ToolSpec(
            name="session_state",
            required_permissions=(),
            connector=dummy,
            tier=Tier.T0,
        )
    )
    return reg


def test_build_runtime_wires_session_provider() -> None:
    from agents_system.harness.factory import build_runtime

    reg = _full_sales_registry()
    sentinel = object()

    # With session_provider — should be wired through
    runtime = build_runtime(
        "sales-agent",
        reg,
        [
            "read:catalog",
            "read:client_registry",
            "write:orders",
            "write:order_items",
            "send:message",
        ],
        client="client-a",
        roots=_client_a_roots(),
        session_provider=sentinel,
    )
    assert runtime.session_provider is sentinel

    # Without session_provider — should default to None
    runtime2 = build_runtime(
        "sales-agent",
        reg,
        ["read:catalog"],
        client="client-a",
        roots=_client_a_roots(),
    )
    assert runtime2.session_provider is None
