"""Tests for agentsys.config — Settings loading and singleton."""

import pytest
from pydantic import ValidationError

from agentsys.config import Settings, get_settings


def test_settings_loads_with_defaults():
    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.debug is False
    assert "postgresql" in settings.database_url
    assert settings.rag_threshold_direct == 0.60
    assert settings.rag_threshold_ambiguous == 0.50
    assert settings.rag_top_k == 3
    assert settings.rag_keyword_top_k == 5
    assert settings.rag_hnsw_ef_search == 40


def test_rag_thresholds_overridable_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_THRESHOLD_DIRECT", "0.70")
    monkeypatch.setenv("RAG_THRESHOLD_AMBIGUOUS", "0.55")
    settings = Settings(_env_file=None)
    assert settings.rag_threshold_direct == 0.70
    assert settings.rag_threshold_ambiguous == 0.55


@pytest.mark.parametrize(
    ("direct", "ambiguous"),
    [
        (0.82, 0.82),
        (0.81, 0.82),
    ],
)
def test_settings_reject_invalid_rag_threshold_order(
    direct: float, ambiguous: float
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            _env_file=None,
            rag_threshold_direct=direct,
            rag_threshold_ambiguous=ambiguous,
        )

    assert "rag_threshold_direct" in str(excinfo.value)
    assert "rag_threshold_ambiguous" in str(excinfo.value)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("rag_top_k", 0),
        ("rag_keyword_top_k", -1),
        ("rag_hnsw_ef_search", 0),
    ],
)
def test_settings_reject_non_positive_rag_limits(field_name: str, value: int) -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **{field_name: value})

    assert field_name in str(excinfo.value)


def test_get_settings_returns_singleton():
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


# ---------------------------------------------------------------------------
# D-012 — adapter config fields
# ---------------------------------------------------------------------------


def test_adapter_config_defaults():
    """adapter_api_key, adapter_provider, and adapter_runtimes have correct defaults."""
    settings = Settings(_env_file=None)
    assert settings.adapter_api_key == ""
    assert settings.adapter_provider == "ollama"
    assert settings.adapter_runtimes == ["badie__sales-agent"]


# ---------------------------------------------------------------------------
# D-014 S4 — checkpointer/persistence config fields (design AD-7)
# ---------------------------------------------------------------------------


def test_whatsapp_checkpointer_config_defaults():
    """whatsapp_checkpointer_enabled defaults True, checkpointer_ttl_s defaults 86400s."""
    settings = Settings(_env_file=None)
    assert settings.whatsapp_checkpointer_enabled is True
    assert settings.checkpointer_ttl_s == 86400


def test_whatsapp_checkpointer_config_overridable_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WHATSAPP_CHECKPOINTER_ENABLED", "false")
    monkeypatch.setenv("CHECKPOINTER_TTL_S", "3600")
    settings = Settings(_env_file=None)
    assert settings.whatsapp_checkpointer_enabled is False
    assert settings.checkpointer_ttl_s == 3600


def test_checkpointer_ttl_s_accepts_none() -> None:
    """checkpointer_ttl_s=None means no expiry (design AD-7 idle expiry is optional)."""
    settings = Settings(_env_file=None, checkpointer_ttl_s=None)
    assert settings.checkpointer_ttl_s is None
