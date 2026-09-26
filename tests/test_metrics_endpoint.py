"""Tests for GET /metrics (#78 Phase 0 Slice 2).

Same isolation strategy as tests/test_openai_adapter.py: TestClient is built
WITHOUT the context-manager form so the real lifespan never runs, and
agents_system.main.get_settings is patched so the route's Depends(get_settings)
sees a controlled fake Settings -- the real Settings' own fail-closed
validator (metrics_enabled + empty metrics_api_key) is covered separately in
tests/test_config.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from conftest import create_test_app
from fastapi.testclient import TestClient

from agents_system.config import get_settings


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _make_client(
    *,
    metrics_enabled: bool,
    metrics_api_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    app = create_test_app()
    app.state.engine = MagicMock()

    import agents_system.main as main_mod

    fake_settings = MagicMock()
    fake_settings.metrics_enabled = metrics_enabled
    fake_settings.metrics_api_key = metrics_api_key
    monkeypatch.setattr(main_mod, "get_settings", lambda: fake_settings)

    return TestClient(app)


def test_metrics_disabled_by_default_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(
        metrics_enabled=False, metrics_api_key="", monkeypatch=monkeypatch
    )
    response = client.get("/metrics")
    assert response.status_code == 404


def test_metrics_enabled_missing_bearer_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(
        metrics_enabled=True, metrics_api_key="secret", monkeypatch=monkeypatch
    )
    response = client.get("/metrics")
    assert response.status_code == 401


def test_metrics_enabled_wrong_bearer_401(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(
        metrics_enabled=True, metrics_api_key="secret", monkeypatch=monkeypatch
    )
    response = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_metrics_enabled_correct_bearer_200_prometheus_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _make_client(
        metrics_enabled=True, metrics_api_key="secret", monkeypatch=monkeypatch
    )
    response = client.get("/metrics", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    # The process collector's own metrics are always present, regardless of
    # any turn/tool activity in this process.
    assert "process_resident_memory_bytes" in response.text
    assert "process_cpu_seconds_total" in response.text


def test_metrics_enabled_without_key_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """metrics_api_key="" (unset) + metrics_enabled=True -> open, no auth
    enforced (the real Settings' own fail-closed validator is what refuses
    this combination unless allow_insecure=True is also set at boot -- see
    tests/test_config.py; this route-level test exercises the dependency's
    own behavior in isolation, the same way test_openai_adapter.py's
    test_bearer_auth_no_key_configured_open does for verify_bearer)."""
    client = _make_client(
        metrics_enabled=True, metrics_api_key="", monkeypatch=monkeypatch
    )
    response = client.get("/metrics")
    assert response.status_code == 200
