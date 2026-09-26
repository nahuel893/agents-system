"""Tests for the production server entrypoint (agents_system.server)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI

from agents_system.harness.registry import ToolRegistry


def custom_test_factory(settings=None, embedder=None, bi_engine=None) -> ToolRegistry:
    """Fixture factory for testing custom REGISTRY_FACTORY resolution."""
    registry = ToolRegistry()
    return registry


def role_equipping_factory(
    settings=None, embedder=None, bi_engine=None
) -> ToolRegistry:
    """Fixture factory that equips all tools required by sales-agent."""
    from agents_system.harness.registry import Tier, ToolSpec

    registry = ToolRegistry()

    async def _dummy_connector(*args, **kwargs):
        return {}

    registry.register(
        ToolSpec(
            name="session_state",
            description="Manage session state",
            required_permissions=(),
            tier=Tier.T0,
            connector=_dummy_connector,
        )
    )
    registry.register(
        ToolSpec(
            name="escalation_notifier",
            description="Escalate to human",
            required_permissions=(),
            tier=Tier.T0,
            connector=_dummy_connector,
        )
    )
    registry.register(
        ToolSpec(
            name="catalog_search",
            description="Search catalog",
            required_permissions=("read:catalog",),
            tier=Tier.T1,
            connector=_dummy_connector,
        )
    )
    registry.register(
        ToolSpec(
            name="client_lookup",
            description="Lookup client",
            required_permissions=("read:client_registry",),
            tier=Tier.T1,
            connector=_dummy_connector,
        )
    )
    registry.register(
        ToolSpec(
            name="order_writer",
            description="Write order",
            required_permissions=("write:orders",),
            tier=Tier.T2,
            connector=_dummy_connector,
        )
    )
    registry.register(
        ToolSpec(
            name="message_sender",
            description="Send message",
            required_permissions=("send:message",),
            tier=Tier.T2,
            connector=_dummy_connector,
        )
    )
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


def test_get_application_non_callable_registry_factory_raises_type_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REGISTRY_FACTORY pointing to a non-callable attribute raises TypeError."""
    monkeypatch.setenv("ALLOW_INSECURE", "true")
    monkeypatch.setenv("REGISTRY_FACTORY", "sys:version")

    from agents_system.server import get_application

    with pytest.raises(TypeError, match="resolved attribute is not callable"):
        get_application()


def _fake_checkpointer_cm_factory(fake_checkpointer: Any) -> Any:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _cm(*args: Any, **kwargs: Any) -> Any:
        yield fake_checkpointer

    return _cm


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from agents_system.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def mock_lifespan_deps():
    from unittest.mock import AsyncMock, MagicMock, patch

    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()
    with (
        patch("agents_system.main.get_engine", return_value=mock_engine),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch("agents_system.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agents_system.main._build_checkpointer_cm",
            side_effect=_fake_checkpointer_cm_factory(MagicMock()),
        ),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
    ):
        yield


def test_server_lifespan_boots_with_no_agents(
    monkeypatch: pytest.MonkeyPatch,
    mock_lifespan_deps: None,
) -> None:
    """Lifespan boots cleanly when no agents are registered."""
    monkeypatch.setenv("ALLOW_INSECURE", "true")
    monkeypatch.delenv("AGENT_REGISTRATIONS", raising=False)
    monkeypatch.delenv("REGISTRY_FACTORY", raising=False)

    from starlette.testclient import TestClient

    from agents_system.server import get_application

    app = get_application()
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert app.state.runtimes == {}


def test_server_lifespan_fails_when_agent_registered_without_registry_factory(
    monkeypatch: pytest.MonkeyPatch,
    mock_lifespan_deps: None,
) -> None:
    """Booting with registered agents but no REGISTRY_FACTORY fails with clear configuration error."""
    monkeypatch.setenv("ALLOW_INSECURE", "true")
    monkeypatch.setenv("AGENT_REGISTRATIONS", '{"sales": "sales-agent"}')
    monkeypatch.setenv("DEPLOY_GRANTS", '{"sales": ["read:catalog"]}')
    monkeypatch.delenv("REGISTRY_FACTORY", raising=False)

    from starlette.testclient import TestClient

    from agents_system.harness.loader import DefinitionError
    from agents_system.server import get_application

    app = get_application()
    with pytest.raises(DefinitionError) as exc_info, TestClient(app):
        pass

    err_msg = str(exc_info.value)
    assert (
        "Deployments must supply a registry factory providing every tool their agents declare"
        in err_msg
    )
    assert "session_state" in err_msg
    assert "catalog_search" in err_msg
    assert "examples/demo/app.py" in err_msg


def test_server_lifespan_boots_with_custom_registry_factory_equipping_role(
    monkeypatch: pytest.MonkeyPatch,
    mock_lifespan_deps: None,
) -> None:
    """Lifespan boots and equips the registered role when a valid REGISTRY_FACTORY is configured."""
    monkeypatch.setenv("ALLOW_INSECURE", "true")
    monkeypatch.setenv("WHATSAPP_CHECKPOINTER_ENABLED", "false")
    monkeypatch.setenv("AGENT_REGISTRATIONS", '{"sales": "sales-agent"}')
    monkeypatch.setenv(
        "DEPLOY_GRANTS",
        '{"sales": ["read:catalog", "read:client_registry", "write:orders", "send:message"]}',
    )
    monkeypatch.setenv("REGISTRY_FACTORY", "tests.test_server:role_equipping_factory")

    from starlette.testclient import TestClient

    from agents_system.server import get_application

    app = get_application()
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert "sales" in app.state.runtimes


def test_server_module_exports_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """agents_system.server exports `app` for uvicorn."""
    monkeypatch.setenv("ALLOW_INSECURE", "true")

    from agents_system import server

    assert hasattr(server, "app")
    assert isinstance(server.app, FastAPI)
