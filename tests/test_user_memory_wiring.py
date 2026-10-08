"""Per-user memory in the served app and the settings (#250)."""

from __future__ import annotations

import pytest

from agents_system.config import Settings
from agents_system.main import _bind_user_fact_store
from agents_system.scope import InMemoryScopeProvider
from agents_system.server import default_registry_factory
from agents_system.subject_data import SubjectDataRegistry
from agents_system.user_memory import (
    InMemoryUserFactStore,
    UserMemoryLimits,
    limits_from_settings,
)


def _settings(**kw: object) -> Settings:
    return Settings(allow_insecure=True, **kw)  # type: ignore[arg-type]


def test_limits_default_to_the_documented_values() -> None:
    assert limits_from_settings(_settings()) == UserMemoryLimits(
        max_facts=50, max_fact_bytes=500, max_injected_chars=1_500, ttl_s=None
    )


def test_limits_come_from_user_memory_settings_and_the_facts_retention() -> None:
    limits = limits_from_settings(
        _settings(
            user_memory_max_facts=7,
            user_memory_max_fact_bytes=80,
            user_memory_max_injected_chars=300,
            subject_data_retention={"user_facts": 3600, "usage": "never"},
        )
    )
    assert limits == UserMemoryLimits(7, 80, 300, 3600)


def test_the_facts_retention_may_be_never() -> None:
    settings = _settings(subject_data_retention={"user_facts": "never"})
    assert limits_from_settings(settings).ttl_s is None


def test_a_non_positive_limit_is_refused() -> None:
    with pytest.raises(ValueError):
        _settings(user_memory_max_facts=0)


def test_the_store_is_registered_so_export_and_delete_reach_it() -> None:
    registry = SubjectDataRegistry()
    _bind_user_fact_store(InMemoryScopeProvider(), registry)
    assert isinstance(registry.stores["user_facts"], InMemoryUserFactStore)


def test_without_a_scope_provider_nothing_is_registered() -> None:
    registry = SubjectDataRegistry()
    _bind_user_fact_store(None, registry)
    assert registry.stores == {}


def test_the_default_registry_carries_the_three_tools() -> None:
    registry = default_registry_factory()
    for name in ("remember_user_fact", "forget_user_fact", "list_user_facts"):
        assert name in registry


def test_the_permissions_are_registered_with_the_right_tiers() -> None:
    from agents_system.harness.registry import Tier
    from agents_system.permissions import permission_registry

    assert permission_registry.resolve("read:user_memory").tier is Tier.T0
    assert permission_registry.resolve("write:user_memory").tier is Tier.T2
