"""Every migrated table with a ``subject_key`` column has a registered store (#257).

Offline guard: scans the Alembic revisions, so adding a per-user table in a
migration without registering a store in the default registry fails here
without needing a database.
"""

from __future__ import annotations

import re
from pathlib import Path

# Importing a store module registers it; add each per-user store here.
import agents_system.services.usage_postgres
from agents_system.subject_data import DEFAULT_REGISTRY

import agents_system.services.user_facts_postgres  # noqa: F401  isort:skip

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_TABLE_CALL = re.compile(
    r"""(?:create_table|add_column)\(\s*["'](\w+)["'](.*?)(?:\n\s*\)|\Z)""", re.DOTALL
)


def migrated_subject_tables() -> set[str]:
    tables: set[str] = set()
    for path in _VERSIONS.glob("*.py"):
        for name, body in _TABLE_CALL.findall(path.read_text()):
            if "subject_key" in body:
                tables.add(name)
    return tables


def test_scanner_finds_a_subject_key_table() -> None:
    sample = 'op.create_table("notes", sa.Column("subject_key", sa.Text))'
    assert [n for n, b in _TABLE_CALL.findall(sample) if "subject_key" in b] == [
        "notes"
    ]


def test_every_migrated_subject_table_has_a_registered_store() -> None:
    assert migrated_subject_tables() - set(DEFAULT_REGISTRY.tables) == set()


def test_the_revisions_directory_is_found() -> None:
    assert any(_VERSIONS.glob("*.py"))


def test_the_proactive_queue_table_is_registered_and_migrated() -> None:
    # Migration 010 creates the table through generated DDL, which the
    # regex scanner above cannot see, so this pins it by name.
    from agents_system.services.proactive_postgres import TABLE

    assert TABLE in DEFAULT_REGISTRY.tables
    assert "010" in {p.name[:3] for p in _VERSIONS.glob("*.py")}
