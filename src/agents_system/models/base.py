"""SQLAlchemy async engine, session factory, and declarative base."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import ClassVar

from agents_system.extras import raise_missing_extra

try:
    from sqlalchemy import DateTime, Table
    from sqlalchemy.ext.asyncio import (
        AsyncEngine,
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.orm import DeclarativeBase
except ImportError as exc:
    raise_missing_extra("postgres", "The platform database layer", exc)

from agents_system.config import Settings, get_settings

#: Key a model sets in its ``Table.info`` to declare that Alembic — not the
#: ORM — owns its DDL. See ``alembic_owned_tables`` for why that matters.
ALEMBIC_OWNED = "alembic_owned"


class Base(DeclarativeBase):
    """Base class for all ORM models.

    ``type_annotation_map`` is load-bearing, not tidiness. SQLAlchemy derives a
    column's type from its ``Mapped[...]`` annotation when ``mapped_column``
    receives no explicit type, and its stock answer for ``datetime`` is
    ``DateTime()`` — which is ``timezone=False``, i.e. TIMESTAMP WITHOUT TIME
    ZONE.

    Every migration in this project creates TIMESTAMPTZ, so that default is
    always wrong here, and it fails in the worst possible way: nothing raises.
    The INSERT simply compiles with a ``::TIMESTAMP WITHOUT TIME ZONE`` cast,
    and asyncpg then rejects any aware datetime with "can't subtract
    offset-naive and offset-aware datetimes".

    That is not hypothetical. ``audit_event.occurred_at`` was written as a bare
    ``Mapped[datetime]``, and the audit log recorded NOTHING against real
    PostgreSQL until D-043 — while all 634 tests passed, because the unit suite
    mocks the session and the migration tests assert on DDL rather than driving
    an ORM INSERT.

    Mapping the annotation here makes the default correct for every model,
    including ones not yet written. The explicit ``DateTime(timezone=True)``
    calls in ``tables.py`` are now redundant; they are kept because they are
    correct and removing them is churn, not because they are needed.
    """

    # ClassVar: this is SQLAlchemy's own class-level configuration attribute,
    # not a per-instance mutable default.
    type_annotation_map: ClassVar[dict[type, DateTime]] = {
        datetime: DateTime(timezone=True)
    }


def alembic_owned_tables() -> tuple[Table, ...]:
    """The tables whose DDL Alembic owns, discovered by their own declaration.

    A table opts in by setting ``{"info": {ALEMBIC_OWNED: True}}`` in its
    ``__table_args__``, so adding another one requires no edit here.

    Some DDL cannot be expressed in the ORM at all — ``audit_event`` is RANGE
    partitioned, and SQLAlchemy has no construct for that. Creating such a
    table from metadata does not raise on PostgreSQL; it silently produces an
    UNPARTITIONED table that differs from what the migration builds, which is
    worse than an error because nothing reports it. On SQLite it fails
    outright, on the composite primary key. So metadata-driven creation must
    skip these tables everywhere, and this is the single place that knows
    which ones they are. Alembic creates them for real.

    Until #70, this fed ``create_orm_owned_tables`` (``Base.metadata.create_all``
    minus this set), used by ``scripts/init_db.py`` to bulk-create the
    platform's other, ORM-owned tables. That helper was removed once
    ``audit_event`` became ``Base.metadata``'s only declared table: excluding
    the only table it knew about left it creating nothing, which is not a
    fixture worth keeping around. A future ORM-owned (non-Alembic) table
    should reintroduce that helper rather than resurrect this comment.
    """
    return tuple(
        table
        for table in Base.metadata.sorted_tables
        if table.info.get(ALEMBIC_OWNED) is True
    )


def _create_engine(
    url: str,
    *,
    pool_size: int,
    max_overflow: int,
    pool_timeout: int,
    pool_recycle: int,
) -> AsyncEngine:
    """Build one bounded, pre-pinging async engine."""
    return create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_timeout=pool_timeout,
        pool_recycle=pool_recycle,
    )


def get_engine(url: str, settings: Settings | None = None) -> AsyncEngine:
    """Create the operational engine using its configured pool limits."""
    settings = settings or get_settings()
    return _create_engine(
        url,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_s,
        pool_recycle=settings.database_pool_recycle_s,
    )


def get_bi_engine(url: str, settings: Settings | None = None) -> AsyncEngine:
    """Create the dedicated BI engine using its configured pool limits."""
    settings = settings or get_settings()
    return _create_engine(
        url,
        pool_size=settings.bi_pool_size,
        max_overflow=settings.bi_max_overflow,
        pool_timeout=settings.bi_pool_timeout_s,
        pool_recycle=settings.bi_pool_recycle_s,
    )


def get_sql_engine(url: str, settings: Settings | None = None) -> AsyncEngine:
    """Create the dedicated SQL-tool engine with its intentionally small pool."""
    settings = settings or get_settings()
    return _create_engine(
        url,
        pool_size=settings.sql_pool_size,
        max_overflow=settings.sql_max_overflow,
        pool_timeout=settings.sql_pool_timeout_s,
        pool_recycle=settings.sql_pool_recycle_s,
    )


def get_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return a session factory bound to *engine*."""
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def get_db(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield an ``AsyncSession`` and close it on exit."""
    session = factory()
    try:
        yield session
    finally:
        await session.close()
