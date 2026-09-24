"""Integration tests for platform-owned reports through the read-only role.

Executed against the application database through ``bi_readonly`` (AD-3). The
fixture below seeds only platform-owned ``audit_event`` rows through the
full-access application engine, then all report execution uses the dedicated
read-only engine. Portable sales-report behavior is exercised separately
against a contract-conforming company database by
``tests/test_sales_reports_integration.py`` in the ``demo-reports`` CI job.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
from sqlalchemy import Integer, String, bindparam, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from agents_system.models.audit_event import map_to_audit_event
from agents_system.models.base import get_engine
from agents_system.services.reports import (
    HARD_ROW_CEILING,
    ParamSpec,
    ReportSpec,
    run_report,
)

pytestmark = pytest.mark.integration


def _require_bi_database_url() -> str:
    url = os.environ.get("BI_DATABASE_URL")
    if not url:
        pytest.skip(
            "BI_DATABASE_URL not set - export the bi_readonly connection string "
            "to run these tests."
        )
    return url


def _require_database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip(
            "DATABASE_URL not set - export the full-access connection string "
            "to seed platform-owned fixture data for these tests."
        )
    return url


@pytest.fixture
async def bi_engine() -> AsyncIterator[AsyncEngine]:
    url = _require_bi_database_url()
    engine = get_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def admin_engine() -> AsyncIterator[AsyncEngine]:
    """Full-access engine used only to seed platform-owned fixture data."""
    url = _require_database_url()
    engine = get_engine(url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def seeded_audit_event_correlation_id(admin_engine: AsyncEngine) -> str:
    """Insert a deterministic, varied platform-owned ``audit_event`` fixture.

    Three rows make grouping, ordering, filtering, and limit-clamping tests
    non-vacuous while keeping the fixture isolated by its unique correlation
    ID. The read-only engine is never used for setup.
    """
    correlation_id = f"bi-readonly-fixture-{uuid.uuid4()}"
    events = (
        (1, "bi_readonly_fixture_probe", "test"),
        (2, "bi_readonly_fixture_secondary", "worker"),
        (3, "bi_readonly_fixture_probe", "test"),
    )
    rows = [
        map_to_audit_event(
            {
                "event_id": uuid.uuid4(),
                "correlation_id": correlation_id,
                "sequence": sequence,
                "event_type": event_type,
                "role": role,
                "payload": {"source": "test_reports_integration"},
            }
        )
        for sequence, event_type, role in events
    ]
    session_factory = async_sessionmaker(admin_engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(rows)
        await session.commit()
    return correlation_id


_CORRELATION_ID_PARAM = ParamSpec(
    name="correlation_id",
    type=str,
    required=True,
    description="correlation_id of the seeded audit_event rows.",
)
_LIMIT_PARAM = ParamSpec(
    name="limit",
    type=int,
    default=10,
    minimum=1,
    description="Maximum rows to return.",
)


def _role_filter_metadata(
    validated: Mapping[str, Any], _bind_params: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Expose the value that the role-filtered query actually used."""
    return {"roles_included": [validated["role"]]}


_AUDIT_EVENT_SAMPLE_SPEC = ReportSpec(
    name="audit_event_sample",
    description="Return fixture audit events by correlation ID.",
    sql=text(
        """
        SELECT event_type, role, tool_name, occurred_at
        FROM audit_event
        WHERE correlation_id = :correlation_id
        ORDER BY sequence
        LIMIT :limit
        """
    ).bindparams(bindparam("correlation_id", type_=String())),
    params=(_CORRELATION_ID_PARAM, _LIMIT_PARAM),
)

_AUDIT_EVENT_TIMELINE_SPEC = ReportSpec(
    name="audit_event_timeline",
    description="Group fixture audit events by month.",
    sql=text(
        """
        SELECT date_trunc('month', occurred_at) AS month, COUNT(*) AS event_count
        FROM audit_event
        WHERE correlation_id = :correlation_id
        GROUP BY 1
        ORDER BY 1 ASC
        LIMIT :limit
        """
    ).bindparams(bindparam("correlation_id", type_=String())),
    params=(_CORRELATION_ID_PARAM, _LIMIT_PARAM),
)

_AUDIT_EVENT_BY_ROLE_SPEC = ReportSpec(
    name="audit_event_by_role",
    description="Count fixture audit events by role.",
    sql=text(
        """
        SELECT role, COUNT(*) AS event_count
        FROM audit_event
        WHERE correlation_id = :correlation_id
        GROUP BY role
        ORDER BY event_count DESC, role ASC
        LIMIT :limit
        """
    ).bindparams(bindparam("correlation_id", type_=String())),
    params=(_CORRELATION_ID_PARAM, _LIMIT_PARAM),
)

_AUDIT_EVENT_BY_TYPE_AND_ROLE_SPEC = ReportSpec(
    name="audit_event_by_type_and_role",
    description="Count fixture audit events by event type and role.",
    sql=text(
        """
        SELECT event_type, role, COUNT(*) AS event_count
        FROM audit_event
        WHERE correlation_id = :correlation_id
        GROUP BY event_type, role
        ORDER BY event_count DESC, event_type ASC, role ASC
        LIMIT :limit
        """
    ).bindparams(bindparam("correlation_id", type_=String())),
    params=(_CORRELATION_ID_PARAM, _LIMIT_PARAM),
)

_AUDIT_EVENT_TYPE_RANKING_SPEC = ReportSpec(
    name="audit_event_type_ranking",
    description="Rank fixture event types by their event count.",
    sql=text(
        """
        SELECT event_type, COUNT(*) AS event_count
        FROM audit_event
        WHERE correlation_id = :correlation_id
        GROUP BY event_type
        ORDER BY event_count DESC, event_type ASC
        LIMIT :limit
        """
    ).bindparams(bindparam("correlation_id", type_=String())),
    params=(_CORRELATION_ID_PARAM, _LIMIT_PARAM),
)

_AUDIT_EVENT_BY_FILTERED_ROLE_SPEC = ReportSpec(
    name="audit_event_by_filtered_role",
    description="Return fixture audit events for one requested role.",
    sql=text(
        """
        SELECT event_type, role, sequence
        FROM audit_event
        WHERE correlation_id = :correlation_id AND role = :role
        ORDER BY sequence ASC
        LIMIT :limit
        """
    ).bindparams(bindparam("correlation_id", type_=String())),
    params=(
        _CORRELATION_ID_PARAM,
        ParamSpec(name="role", type=str, required=True, description="Role to include."),
        _LIMIT_PARAM,
    ),
    filter_metadata=_role_filter_metadata,
)

_AUDIT_EVENT_EXPANDED_ROWS_SPEC = ReportSpec(
    name="audit_event_expanded_rows",
    description="Expand fixture audit events to exercise the hard row ceiling.",
    sql=text(
        """
        WITH matching_events AS (
            SELECT event_id
            FROM audit_event
            WHERE correlation_id = :correlation_id
        )
        SELECT row_number() OVER (ORDER BY matching_events.event_id, copies.n) AS row_number
        FROM matching_events
        CROSS JOIN generate_series(1, 200) AS copies(n)
        ORDER BY row_number
        LIMIT :limit
        """
    ).bindparams(
        bindparam("correlation_id", type_=String()),
        bindparam("limit", type_=Integer()),
    ),
    params=(_CORRELATION_ID_PARAM, _LIMIT_PARAM),
)


async def test_event_timeline_returns_rows_with_expected_shape(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """Preserves the monthly aggregate result-shape property."""
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_TIMELINE_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 50},
    )

    assert result["row_count"] == 1
    assert set(result["rows"][0]) == {"month", "event_count"}


async def test_event_type_summary_returns_rows_with_expected_shape(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """Preserves grouped aggregate rows with a named dimension."""
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_TYPE_RANKING_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 5},
    )

    assert result["row_count"] == 2
    assert set(result["rows"][0]) == {"event_type", "event_count"}


async def test_event_role_summary_returns_rows_with_expected_shape(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """Preserves grouping by a second platform-owned dimension."""
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_BY_ROLE_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 20},
    )

    assert result["row_count"] == 2
    assert set(result["rows"][0]) == {"role", "event_count"}


async def test_event_type_role_summary_returns_rows_with_expected_shape(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """Preserves multi-dimension grouping and its result shape."""
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_BY_TYPE_AND_ROLE_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 20},
    )

    assert result["row_count"] == 2
    assert set(result["rows"][0]) == {"event_type", "role", "event_count"}


async def test_event_type_ranking_orders_descending_by_count(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """Preserves the ranked-result property of the former top-items report."""
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_TYPE_RANKING_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 10},
    )

    counts = [row["event_count"] for row in result["rows"]]
    assert counts == sorted(counts, reverse=True)
    assert result["rows"][0]["event_type"] == "bi_readonly_fixture_probe"


async def test_event_type_summary_reports_all_seeded_categories(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """Preserves exact grouped-category reporting without client statuses.

    Normalized sale-status behavior now belongs to the portable catalog and is
    exercised by ``test_status_summary_recovers_the_seeded_status_mix`` in the
    ``demo-reports`` job.
    """
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_TYPE_RANKING_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 10},
    )

    counts = {row["event_type"]: row["event_count"] for row in result["rows"]}
    assert counts == {
        "bi_readonly_fixture_probe": 2,
        "bi_readonly_fixture_secondary": 1,
    }


async def test_row_limit_is_clamped_to_hard_ceiling(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """Preserves the end-to-end hard-ceiling property with over 500 rows."""
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_EXPANDED_ROWS_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 999_999},
    )

    assert result["row_count"] == HARD_ROW_CEILING


async def test_role_filter_changes_rows_and_discloses_the_applied_filter(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """Preserves parameterized filtering plus derived filter disclosure.

    The portable catalog separately exercises its sale-status semantics in the
    contract-conforming ``demo-reports`` database.
    """
    test_role = await run_report(
        bi_engine,
        _AUDIT_EVENT_BY_FILTERED_ROLE_SPEC,
        {
            "correlation_id": seeded_audit_event_correlation_id,
            "role": "test",
            "limit": 10,
        },
    )
    worker_role = await run_report(
        bi_engine,
        _AUDIT_EVENT_BY_FILTERED_ROLE_SPEC,
        {
            "correlation_id": seeded_audit_event_correlation_id,
            "role": "worker",
            "limit": 10,
        },
    )

    assert test_role["row_count"] == 2
    assert worker_role["row_count"] == 1
    assert test_role["meta"]["roles_included"] == ["test"]
    assert worker_role["meta"]["roles_included"] == ["worker"]


async def test_audit_event_report_spec_returns_expected_shape(
    bi_engine: AsyncEngine, seeded_audit_event_correlation_id: str
) -> None:
    """The generic report path runs against a platform-owned relation."""
    result = await run_report(
        bi_engine,
        _AUDIT_EVENT_SAMPLE_SPEC,
        {"correlation_id": seeded_audit_event_correlation_id, "limit": 10},
    )

    assert result["row_count"] == 3
    assert set(result["rows"][0]) == {"event_type", "role", "tool_name", "occurred_at"}
    assert [row["event_type"] for row in result["rows"]] == [
        "bi_readonly_fixture_probe",
        "bi_readonly_fixture_secondary",
        "bi_readonly_fixture_probe",
    ]


async def test_bi_readonly_engine_refuses_writes(bi_engine: AsyncEngine) -> None:
    """Defence in depth: database-role permissions reject a direct INSERT."""
    with pytest.raises(Exception) as exc:
        async with bi_engine.connect() as conn:
            await conn.execute(
                text(
                    "INSERT INTO audit_event "
                    "(event_id, correlation_id, sequence, event_type, payload) "
                    "VALUES (:event_id, :correlation_id, :sequence, :event_type, "
                    "CAST(:payload AS jsonb))"
                ),
                {
                    "event_id": "00000000-0000-0000-0000-000000000000",
                    "correlation_id": "should-not-persist",
                    "sequence": 0,
                    "event_type": "should-not-persist",
                    "payload": "{}",
                },
            )
    assert "read-only" in str(exc.value).lower()
