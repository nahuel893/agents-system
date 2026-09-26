"""Tests for the production server entrypoint (agents_system.server)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from agents_system.harness.registry import ToolRegistry


def custom_test_factory(settings=None, embedder=None, bi_engine=None) -> ToolRegistry:
    """Fixture factory for testing custom REGISTRY_FACTORY resolution."""
    registry = ToolRegistry()
    return registry


def test_default_registry_factory_registers_platform_tools() -> None:
    """Default registry factory registers the standard platform tools in fail-closed/unbound mode."""
    from agents_system.server import default_registry_factory

    registry = default_registry_factory()
    assert isinstance(registry, ToolRegistry)
    tools = set(registry.names())
    assert "order_writer" in tools
    assert "knowledge_retrieval" in tools
    assert "conversation_summarizer" in tools
    assert "escalation_notifier" in tools
    assert "run_report" in tools


def test_get_application_creates_fastapi_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_application builds a FastAPI application equipped with health endpoints."""
    # Ensure no required WhatsApp token triggers boot fail-closed without runtime
    monkeypatch.setenv("ALLOW_INSECURE", "true")

    from agents_system.server import get_application

    app = get_application()
    assert isinstance(app, FastAPI)
    routes = [getattr(route, "path", None) for route in app.routes]
    assert "/health" in routes
    assert "/ready" in routes


def test_get_application_custom_registry_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_application resolves custom registry factory from REGISTRY_FACTORY."""
    monkeypatch.setenv("ALLOW_INSECURE", "true")
    monkeypatch.setenv("REGISTRY_FACTORY", "tests.test_server:custom_test_factory")

    from agents_system.server import get_application

    app = get_application()
    assert isinstance(app, FastAPI)
    assert app.state.registry_factory.__name__ == custom_test_factory.__name__


def test_get_application_invalid_registry_factory_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed REGISTRY_FACTORY string fails loudly with ValueError."""
    monkeypatch.setenv("REGISTRY_FACTORY", "invalid_format_without_colon")

    from agents_system.server import get_application

    with pytest.raises(ValueError, match="expected 'module:callable'"):
        get_application()


def test_server_module_exports_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """agents_system.server exports `app` for uvicorn."""
    monkeypatch.setenv("ALLOW_INSECURE", "true")

    from agents_system import server

    assert hasattr(server, "app")
    assert isinstance(server.app, FastAPI)
