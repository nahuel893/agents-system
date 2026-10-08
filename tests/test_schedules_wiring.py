"""Schedules in the served app, the settings and the permission model (#175)."""

from __future__ import annotations

from datetime import time

import pytest

from agents_system.config import Settings
from agents_system.harness.registry import Tier
from agents_system.main import _bind_schedule_store
from agents_system.permissions import resolve
from agents_system.schedules import (
    InMemoryScheduleStore,
    ScheduleLimits,
    limits_from_settings,
)
from agents_system.scope import InMemoryScopeProvider
from agents_system.server import default_registry_factory
from agents_system.subject_data import SubjectDataRegistry


def _settings(**kw: object) -> Settings:
    return Settings(allow_insecure=True, **kw)  # type: ignore[arg-type]


def test_limits_default_to_the_documented_values() -> None:
    assert limits_from_settings(_settings()) == ScheduleLimits(
        max_per_user=5,
        min_interval_s=3_600,
        quiet_hours=None,
        quiet_tz="UTC",
        default_tz="UTC",
        max_prompt_chars=500,
        missed_grace_s=1_800,
    )


def test_limits_come_from_the_schedule_settings() -> None:
    limits = limits_from_settings(
        _settings(
            schedule_max_per_user=2,
            schedule_min_interval_s=7200,
            schedule_quiet_start="22:00",
            schedule_quiet_end="07:00",
            schedule_timezone="America/Bogota",
            schedule_missed_grace_s=60,
        )
    )
    assert limits.max_per_user == 2
    assert limits.min_interval_s == 7200
    assert limits.quiet_hours == (time(22, 0), time(7, 0))
    assert limits.quiet_tz == limits.default_tz == "America/Bogota"
    assert limits.missed_grace_s == 60


def test_a_non_positive_limit_or_an_unknown_zone_is_refused() -> None:
    with pytest.raises(ValueError):
        _settings(schedule_max_per_user=0)
    with pytest.raises(ValueError):
        limits_from_settings(_settings(schedule_timezone="Mars/Base"))


def test_one_quiet_hours_bound_alone_is_refused() -> None:
    with pytest.raises(ValueError):
        _settings(schedule_quiet_start="22:00")


def test_the_store_is_registered_so_export_and_delete_reach_it() -> None:
    registry = SubjectDataRegistry()
    _bind_schedule_store(InMemoryScopeProvider(), registry)
    assert isinstance(registry.stores["schedules"], InMemoryScheduleStore)


def test_without_a_scope_provider_nothing_is_registered() -> None:
    registry = SubjectDataRegistry()
    _bind_schedule_store(None, registry)
    assert registry.stores == {}


def test_the_default_registry_carries_the_three_tools() -> None:
    registry = default_registry_factory()
    for name in ("create_schedule", "list_schedules", "delete_schedule"):
        assert name in registry


def test_write_schedule_is_a_registered_write_permission() -> None:
    from agents_system.permissions import Write

    assert resolve("write:schedule") is not None
    assert issubclass(resolve("write:schedule"), Write)
    spec = default_registry_factory().get("create_schedule")
    assert spec.tier is Tier.T2
