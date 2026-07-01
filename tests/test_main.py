"""Tests for the FastAPI app factory's lifespan (D-014 slice S1).

Covers:
  - data-driven grants (design AD-5): lifespan calls harness.loader.resolve()
    and passes definition.permissions as granted_permissions to build_runtime,
    instead of a hardcoded role -> permissions map.
  - outbound WhatsApp client (design AD-2): lifespan builds a WhatsAppClient
    and stores it on app.state.whatsapp_client.

No real network / DB / embedder / LLM calls: every heavy dependency the
lifespan touches is patched at its defining module (main.py imports them
lazily inside the function body).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentsys.config import Settings, get_settings
from agentsys.main import create_app, lifespan


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = dict(
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
        adapter_runtimes=["badie__sales-agent"],
        whatsapp_token="test-token",
        whatsapp_phone_number_id="1234567890",
    )
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_lifespan_uses_data_driven_grants() -> None:
    """lifespan resolves the definition FIRST and passes its permissions to
    build_runtime as granted_permissions — not a hardcoded role map."""
    test_settings = _make_settings()

    fake_definition = MagicMock()
    fake_definition.permissions = ("read:catalog", "write:orders")

    fake_equipped = MagicMock()
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agentsys.main.get_settings", return_value=test_settings),
        patch("agentsys.main.get_engine", return_value=mock_engine),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
        patch("agentsys.main._build_chat_model", return_value=MagicMock()),
        patch(
            "agentsys.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.connectors.rag_connector.build_badie_rag_registry",
            return_value=MagicMock(),
        ),
        patch(
            "agentsys.harness.loader.resolve", return_value=fake_definition
        ) as mock_resolve,
        patch(
            "agentsys.harness.factory.build_runtime", return_value=fake_equipped
        ) as mock_build_runtime,
        patch("agentsys.agent.graph.AgentRuntime", return_value=MagicMock()),
    ):
        app = create_app()

        async with lifespan(app):
            assert app.state.runtimes

        # resolve() called for the sales-agent role with the badie deployment
        mock_resolve.assert_any_call("sales-agent", client="badie")

        # build_runtime received the resolved definition's permissions —
        # NOT a hardcoded role -> permissions map.
        _, kwargs = mock_build_runtime.call_args
        assert tuple(kwargs["granted_permissions"]) == ("read:catalog", "write:orders")


@pytest.mark.asyncio
async def test_lifespan_builds_whatsapp_client() -> None:
    """lifespan builds a WhatsAppClient and stores it on app.state."""
    test_settings = _make_settings(adapter_runtimes=[])
    mock_engine = MagicMock()
    mock_engine.dispose = AsyncMock()

    with (
        patch("agentsys.main.get_settings", return_value=test_settings),
        patch("agentsys.main.get_engine", return_value=mock_engine),
        patch("agentsys.main.close_redis_pool", new=AsyncMock()),
    ):
        app = create_app()

        async with lifespan(app):
            from agentsys.integration.whatsapp_client import WhatsAppClient

            assert isinstance(app.state.whatsapp_client, WhatsAppClient)
