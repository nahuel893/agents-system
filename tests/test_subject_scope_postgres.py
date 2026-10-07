"""The scope handle fenced by Postgres row-level security (ADR-011 part 3, #256).

Superusers and BYPASSRLS roles skip row-level security even under FORCE, and
the docker Postgres connects as a superuser, so these tests create a
dedicated NOLOGIN-free, non-superuser role and connect as it. They need
`DATABASE_URL` (an admin URL, used only to provision) and skip without it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents_system.scope import assert_subject_isolation
from agents_system.services.scope_postgres import (
    PostgresScopeProvider,
    UnfencedRoleError,
    create_subject_policy,
    subject_policy_statements,
)

pytestmark = pytest.mark.integration


def _admin_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set - see this module's docstring.")
    return url


@pytest.fixture
async def fenced() -> AsyncIterator[tuple[AsyncEngine, AsyncEngine, str]]:
    """(admin engine, app engine on a non-superuser role, table name)."""
    suffix = uuid.uuid4().hex[:8]
    role, table, password = f"rls_app_{suffix}", f"rls_notes_{suffix}", "pw" + suffix
    admin_url = make_url(_admin_url())
    admin = create_async_engine(admin_url)
    async with admin.begin() as conn:
        await conn.execute(
            text(
                f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER NOBYPASSRLS"
            )
        )
        await conn.execute(
            text(
                f"CREATE TABLE {table} (runtime_id text NOT NULL, subject_key text NOT NULL,"
                " body text NOT NULL)"
            )
        )
        await conn.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")
        )
        await create_subject_policy(conn, table)
    app = create_async_engine(admin_url.set(username=role, password=password))
    try:
        yield admin, app, table
    finally:
        await app.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))
        await admin.dispose()


async def test_unfiltered_query_returns_only_the_scoped_subjects_rows(
    fenced: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    _, app, table = fenced
    provider = PostgresScopeProvider(app)

    async def write(scope: Any, value: str) -> None:
        async with scope.transaction() as conn:
            await conn.execute(
                text(
                    f"INSERT INTO {table} (runtime_id, subject_key, body)"
                    " VALUES (:r, :s, :b)"
                ),
                {"r": scope.runtime_id, "s": scope.subject_key, "b": value},
            )

    async def read_all(scope: Any) -> list[str]:
        async with scope.transaction() as conn:
            # Deliberately no WHERE: the policy alone must fence it.
            rows = await conn.execute(text(f"SELECT body FROM {table}"))
            return [r[0] for r in rows]

    await assert_subject_isolation(provider, write=write, read_all=read_all)


async def test_other_runtime_id_sees_nothing(
    fenced: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    _, app, table = fenced
    provider = PostgresScopeProvider(app)
    mine = provider.scope("rt-a", "participant:x")
    async with mine.transaction() as conn:
        await conn.execute(
            text(f"INSERT INTO {table} VALUES ('rt-a', 'participant:x', 'v')")
        )
    other = provider.scope("rt-b", "participant:x")
    async with other.transaction() as conn:
        assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == 0


async def test_write_for_another_subject_is_refused(
    fenced: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    _, app, table = fenced
    scope = PostgresScopeProvider(app).scope("rt", "participant:a")
    with pytest.raises(Exception, match="row-level security"):
        async with scope.transaction() as conn:
            await conn.execute(
                text(f"INSERT INTO {table} VALUES ('rt', 'participant:b', 'v')")
            )


async def test_no_scope_settings_means_no_rows(
    fenced: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    admin, app, table = fenced
    async with admin.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO {table} VALUES ('rt', 'participant:a', 'v')")
        )
    async with app.begin() as conn:  # a plain connection, no scope opened
        assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == 0


async def test_superuser_bypasses_rls_so_the_provider_refuses_it(
    fenced: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    admin, app, table = fenced
    # Proof the premise holds: the admin (superuser) sees the row despite FORCE.
    async with admin.begin() as conn:
        await conn.execute(
            text(f"INSERT INTO {table} VALUES ('rt', 'participant:a', 'v')")
        )
        assert (await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == 1
    with pytest.raises(UnfencedRoleError):
        await PostgresScopeProvider(admin).verify()
    await PostgresScopeProvider(app).verify()  # the dedicated role passes


async def test_preexisting_permissive_policy_does_not_unfence_the_table(
    fenced: tuple[AsyncEngine, AsyncEngine, str],
) -> None:
    admin, app, table = fenced
    # A legacy permissive policy is OR-combined with ours; the subject
    # predicate must therefore also be RESTRICTIVE (AND-combined).
    async with admin.begin() as conn:
        await conn.execute(
            text(f"CREATE POLICY legacy_read ON {table} FOR SELECT USING (true)")
        )
        await conn.execute(
            text(f"INSERT INTO {table} VALUES ('rt', 'participant:b', 'secret')")
        )
    scope = PostgresScopeProvider(app).scope("rt", "participant:a")
    async with scope.transaction() as conn:
        rows = (await conn.execute(text(f"SELECT body FROM {table}"))).all()
    assert rows == []


def test_policy_includes_a_restrictive_subject_predicate() -> None:
    joined = "\n".join(subject_policy_statements("per_user_facts"))
    assert "AS RESTRICTIVE" in joined


def test_policy_statements_force_rls_and_reject_bad_identifiers() -> None:
    statements = subject_policy_statements("per_user_facts")
    joined = "\n".join(statements)
    assert "FORCE ROW LEVEL SECURITY" in joined
    assert "app.runtime_id" in joined and "app.subject_key" in joined
    with pytest.raises(ValueError):
        subject_policy_statements("facts; DROP TABLE x")
