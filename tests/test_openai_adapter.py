"""Tests for the OpenAI-compatible adapter (D-012, slice 1).

Covers:
  - GET /v1/models — happy path (one entry per runtime id)
  - Bearer auth: 401 when key set + missing/wrong token
  - No-auth path when key unset (adapter_api_key == "")

Isolation strategy:
  TestClient is instantiated WITHOUT the context-manager form so the real
  lifespan (which loads BGE-M3 ~570MB and connects to Ollama/DB) never runs.
  Instead we set app.state.runtimes directly before each request and patch
  agentsys.integration.openai_adapter.get_settings so the verify_bearer
  dependency sees a controlled adapter_api_key.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from agentsys.config import get_settings
from agentsys.main import create_app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_runtimes(runtime_ids: list[str]) -> dict[str, MagicMock]:
    """Build a stub runtimes dict keyed by model id."""
    result: dict[str, MagicMock] = {}
    for rid in runtime_ids:
        rt = MagicMock()
        rt.run_turn = AsyncMock(return_value=[MagicMock(content="ok")])
        result[rid] = rt
    return result


def _make_client(
    runtime_ids: list[str],
    adapter_api_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Create a TestClient with pre-populated runtimes and no real lifespan."""
    app = create_app()
    # Set state BEFORE any request — lifespan never fires (no context manager)
    app.state.runtimes = _fake_runtimes(runtime_ids)
    app.state.engine = MagicMock()

    # Patch get_settings at the router module so verify_bearer sees the test key
    import agentsys.integration.openai_adapter as adapter_mod

    fake_settings = MagicMock()
    fake_settings.adapter_api_key = adapter_api_key
    monkeypatch.setattr(adapter_mod, "get_settings", lambda: fake_settings)

    return TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Slice 1 — GET /v1/models
# ---------------------------------------------------------------------------


def test_models_list_200(monkeypatch: pytest.MonkeyPatch):
    """GET /v1/models with valid bearer returns 200 + OpenAI models-list shape."""
    client = _make_client(
        runtime_ids=["badie__sales-agent"],
        adapter_api_key="test-key",
        monkeypatch=monkeypatch,
    )
    response = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    data = body["data"]
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == "badie__sales-agent"
    assert data[0]["object"] == "model"


def test_models_list_multiple_runtimes(monkeypatch: pytest.MonkeyPatch):
    """GET /v1/models returns one entry per configured runtime id."""
    runtime_ids = ["badie__sales-agent", "_generic__sales-agent"]
    client = _make_client(
        runtime_ids=runtime_ids,
        adapter_api_key="",  # open mode
        monkeypatch=monkeypatch,
    )
    response = client.get("/v1/models")
    assert response.status_code == 200
    body = response.json()
    returned_ids = {entry["id"] for entry in body["data"]}
    assert returned_ids == set(runtime_ids)


# ---------------------------------------------------------------------------
# Slice 1 — Bearer auth
# ---------------------------------------------------------------------------


def test_bearer_auth_missing_401(monkeypatch: pytest.MonkeyPatch):
    """API key set + no Authorization header → 401."""
    client = _make_client(
        runtime_ids=["badie__sales-agent"],
        adapter_api_key="my-secret",
        monkeypatch=monkeypatch,
    )
    response = client.get("/v1/models")
    assert response.status_code == 401


def test_bearer_auth_invalid_401(monkeypatch: pytest.MonkeyPatch):
    """API key set + wrong key in header → 401."""
    client = _make_client(
        runtime_ids=["badie__sales-agent"],
        adapter_api_key="correct-key",
        monkeypatch=monkeypatch,
    )
    response = client.get(
        "/v1/models",
        headers={"Authorization": "Bearer wrong-key"},
    )
    assert response.status_code == 401


def test_bearer_auth_no_key_configured_open(monkeypatch: pytest.MonkeyPatch):
    """adapter_api_key="" (unset) + no Authorization header → 200, no auth enforced."""
    client = _make_client(
        runtime_ids=["badie__sales-agent"],
        adapter_api_key="",
        monkeypatch=monkeypatch,
    )
    response = client.get("/v1/models")
    assert response.status_code == 200
