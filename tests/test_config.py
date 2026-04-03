"""Tests for badie.config — Settings loading and singleton."""

from badie.config import Settings, get_settings


def test_settings_loads_with_defaults():
    settings = Settings()
    assert settings.environment == "development"
    assert settings.debug is False
    assert "postgresql" in settings.database_url


def test_get_settings_returns_singleton():
    get_settings.cache_clear()
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
