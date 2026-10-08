"""The proactive queue and the subject-data registry (#251, ADR-011 part 6)."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy")

from agents_system.services import proactive_postgres
from agents_system.services.proactive_postgres import (
    PROACTIVE_STORE,
    PostgresProactiveQueue,
)
from agents_system.services.scope_postgres import PostgresScopeProvider
from agents_system.subject_data import DEFAULT_REGISTRY


@pytest.fixture(autouse=True)
def _restore_store() -> None:
    before = PROACTIVE_STORE._provider
    yield
    PROACTIVE_STORE._provider = before


def test_the_default_registry_holds_the_proactive_store() -> None:
    assert DEFAULT_REGISTRY.stores["proactive"] is proactive_postgres.PROACTIVE_STORE


def test_the_documented_queue_binds_the_registered_store() -> None:
    PROACTIVE_STORE._provider = None
    provider = PostgresScopeProvider(object())  # type: ignore[arg-type]
    queue = PostgresProactiveQueue(provider)
    assert PROACTIVE_STORE._provider is provider
    assert queue is not PROACTIVE_STORE


def test_a_queue_over_another_table_does_not_rebind_the_store() -> None:
    PROACTIVE_STORE._provider = None
    PostgresProactiveQueue(
        PostgresScopeProvider(object()),  # type: ignore[arg-type]
        table="proactive_scratch",
    )
    assert PROACTIVE_STORE._provider is None
