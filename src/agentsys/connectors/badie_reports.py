"""BADIE (client) BI report catalog (D-023).

CLIENT DATA, not platform: six reports over `clients` x `orders` x
`order_items` - the local operational `badie` database. The medallion
warehouse targeted by the D-013 exploration is unreachable in this
environment (`medallion_db_host` is None); this catalog ships the same
architecture against real, seeded, analytically meaningful data instead.
Pointing the platform at medallion later is a catalog + engine swap, not a
rewrite - see `sdd/d-023-bi-agent/design` in engram.

Business-semantics note (read before touching the `status` param): orders
carry `status` in {"confirmed", "pending", "cancelled"}. Whether a
cancelled order counts toward "revenue" changes every number a report
returns. Every report here defaults to EXCLUDING cancelled orders and
states so explicitly in `meta.statuses_included` / `meta.cancelled_included`
- callers must never report a number without checking that field. Pass
`status="all"` to include cancelled orders, or a specific status
("confirmed" / "pending" / "cancelled") to isolate one.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from sqlalchemy import DateTime, Integer, bindparam, text

from agentsys.services.reports import ParamSpec, ReportSpec

ALL_STATUSES: tuple[str, ...] = ("confirmed", "pending", "cancelled")
NON_CANCELLED_STATUSES: tuple[str, ...] = ("confirmed", "pending")

_STATUS_ALLOWED: tuple[str, ...] = ("default", "all", "confirmed", "pending", "cancelled")
_DAYS_PER_MONTH = 30
"""Trailing window granularity for `months_back`: a coarse 30-day multiple,
not a calendar-month boundary. Good enough for a "last N months" analytical
window and avoids pulling in a calendar-arithmetic dependency for it."""


def _resolve_statuses(status: str) -> tuple[str, ...]:
    """Map the caller-facing `status` value onto the concrete statuses to
    filter by. Pure function - no I/O, easy to unit test in isolation."""
    if status in ("default",):
        return NON_CANCELLED_STATUSES
    if status == "all":
        return ALL_STATUSES
    return (status,)


def _utcnow() -> datetime:
    """The single clock seam for this module.

    Exists so a test can count how many times a report run reads the clock.
    Patching `_months_back_to_since` cannot do that: `ParamSpec.transform`
    captures a direct reference to the function at import time, so a
    monkeypatched module global is invisible to the bind path and a test
    written that way silently measures only half the calls.
    """
    return datetime.now(UTC)


def _months_back_to_since(months_back: int, *, now: datetime | None = None) -> datetime:
    """Pure function: `months_back` -> the UTC timestamp to filter from."""
    reference = now or _utcnow()
    return reference - timedelta(days=_DAYS_PER_MONTH * months_back)


def _window_metadata(
    validated: Mapping[str, Any], bind_params: Mapping[str, Any]
) -> dict[str, Any]:
    """Disclose the trailing window the report actually covered.

    `months_back` is a 30-day multiple, not a calendar month, so "the last
    12 months" is really the last 360 days and can exclude rows a reader
    would expect to see. The approximation is fine; leaving it implicit is
    not. A figure whose period the reader cannot see is a figure the reader
    cannot check, so the period travels with the result rather than living
    in a comment next to `_DAYS_PER_MONTH`.

    `window_start` is read off the BOUND `since`, never recomputed. It used
    to call `_months_back_to_since` a second time, after `fetch_rows` had
    already returned — a later clock read describing a window the query
    never used. This function exists precisely so the figure is checkable,
    and a derived disclosure cannot disagree with the query while a
    recomputed one can.
    """
    months_back = validated.get("months_back")
    since = bind_params.get("since")
    if months_back is None or since is None:
        return {}
    return {
        "window_days": _DAYS_PER_MONTH * int(months_back),
        "window_start": since.isoformat(),
        "window_is_calendar_months": False,
    }


def _status_filter_metadata(
    validated: Mapping[str, Any], bind_params: Mapping[str, Any]
) -> dict[str, Any]:
    statuses = _resolve_statuses(validated.get("status") or "default")
    return {
        "statuses_included": list(statuses),
        "cancelled_included": "cancelled" in statuses,
        **_window_metadata(validated, bind_params),
    }


def _all_statuses_metadata(
    validated: Mapping[str, Any], bind_params: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "statuses_included": list(ALL_STATUSES),
        "cancelled_included": True,
        "note": "breakdown by status - not filtered by status.",
        **_window_metadata(validated, bind_params),
    }


def _months_back_param() -> ParamSpec:
    return ParamSpec(
        name="months_back",
        type=int,
        default=12,
        minimum=1,
        maximum=24,
        description="How many trailing months of orders to include (1-24).",
        bind_as="since",
        transform=_months_back_to_since,
    )


def _limit_param(default: int) -> ParamSpec:
    # Deliberately no `maximum` here: AD-5 wants an over-large request to
    # degrade gracefully to the hard ceiling (run_report's clamp_row_limit),
    # not bounce back a validation error. Declaring a maximum here would
    # make validate_params reject it loudly instead - "the ceiling, not an
    # OOM [or an error]". `minimum=1` still rejects nonsensical requests
    # (0 or negative rows), which is a genuine caller mistake, not an
    # "asked for too much" case.
    return ParamSpec(
        name="limit",
        type=int,
        default=default,
        minimum=1,
        description="Maximum rows to return (hard-capped at 500 regardless of this value).",
    )


def _status_param() -> ParamSpec:
    return ParamSpec(
        name="status",
        type=str,
        default="default",
        allowed=_STATUS_ALLOWED,
        description=(
            "Which order statuses to count: 'default' (confirmed + pending, "
            "EXCLUDES cancelled), 'all' (every status, INCLUDES cancelled), "
            "or one specific status ('confirmed' / 'pending' / 'cancelled')."
        ),
        bind_as="statuses",
        transform=_resolve_statuses,
    )


# ---------------------------------------------------------------------------
# Static SQL. AD-2 is absolute: every value below is a named bindparam - the
# `:statuses` list is an "expanding" bindparam (SQLAlchemy's own mechanism
# for variable-arity IN lists) so it stays a bound VALUE, never SQL text,
# even though its length varies with the resolved status filter.
# ---------------------------------------------------------------------------

_VENTAS_POR_MES_SQL = text(
    """
    SELECT
        date_trunc('month', o.created_at) AS month,
        COUNT(*) AS order_count,
        COALESCE(SUM(o.total_amount), 0) AS revenue
    FROM orders o
    WHERE o.status IN :statuses
      AND o.created_at >= :since
    GROUP BY 1
    ORDER BY 1 ASC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_TOP_CLIENTES_SQL = text(
    """
    SELECT
        c.name AS client_name,
        c.zone AS zone,
        COUNT(o.id) AS order_count,
        COALESCE(SUM(o.total_amount), 0) AS revenue
    FROM clients c
    JOIN orders o ON o.client_id = c.id
    WHERE o.status IN :statuses
      AND o.created_at >= :since
    GROUP BY c.id, c.name, c.zone
    ORDER BY revenue DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_VENTAS_POR_ZONA_SQL = text(
    """
    SELECT
        c.zone AS zone,
        COUNT(o.id) AS order_count,
        COALESCE(SUM(o.total_amount), 0) AS revenue,
        COALESCE(AVG(o.total_amount), 0) AS avg_ticket
    FROM clients c
    JOIN orders o ON o.client_id = c.id
    WHERE o.status IN :statuses
      AND o.created_at >= :since
    GROUP BY c.zone
    ORDER BY revenue DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_VENTAS_POR_TIPO_NEGOCIO_SQL = text(
    """
    SELECT
        c.business_type AS business_type,
        COUNT(o.id) AS order_count,
        COALESCE(SUM(o.total_amount), 0) AS revenue,
        COALESCE(AVG(o.total_amount), 0) AS avg_ticket
    FROM clients c
    JOIN orders o ON o.client_id = c.id
    WHERE o.status IN :statuses
      AND o.created_at >= :since
    GROUP BY c.business_type
    ORDER BY revenue DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_TOP_PRODUCTOS_SQL = text(
    """
    SELECT
        oi.sku AS sku,
        MAX(oi.description) AS description,
        SUM(oi.quantity) AS total_quantity,
        COALESCE(SUM(oi.subtotal), 0) AS revenue
    FROM order_items oi
    JOIN orders o ON o.id = oi.order_id
    WHERE o.status IN :statuses
      AND o.created_at >= :since
    GROUP BY oi.sku
    ORDER BY revenue DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_RESUMEN_ESTADOS_SQL = text(
    """
    SELECT
        o.status AS status,
        COUNT(*) AS order_count,
        COALESCE(SUM(o.total_amount), 0) AS revenue
    FROM orders o
    WHERE o.created_at >= :since
    GROUP BY o.status
    ORDER BY order_count DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)


CATALOG: dict[str, ReportSpec] = {
    "ventas_por_mes": ReportSpec(
        name="ventas_por_mes",
        description="Monthly order count and revenue over a trailing window.",
        sql=_VENTAS_POR_MES_SQL,
        params=(_months_back_param(), _limit_param(24), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "top_clientes": ReportSpec(
        name="top_clientes",
        description="Top clients by revenue over a trailing window.",
        sql=_TOP_CLIENTES_SQL,
        params=(_months_back_param(), _limit_param(10), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "ventas_por_zona": ReportSpec(
        name="ventas_por_zona",
        description="Order count, revenue, and average ticket by zone.",
        sql=_VENTAS_POR_ZONA_SQL,
        params=(_months_back_param(), _limit_param(20), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "ventas_por_tipo_negocio": ReportSpec(
        name="ventas_por_tipo_negocio",
        description="Order count, revenue, and average ticket by business type.",
        sql=_VENTAS_POR_TIPO_NEGOCIO_SQL,
        params=(_months_back_param(), _limit_param(20), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "top_productos": ReportSpec(
        name="top_productos",
        description="Top products by revenue over a trailing window.",
        sql=_TOP_PRODUCTOS_SQL,
        params=(_months_back_param(), _limit_param(10), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "resumen_estados": ReportSpec(
        name="resumen_estados",
        description=(
            "Order count and revenue broken down by status - always shows "
            "every status, including cancelled, since that breakdown is "
            "the report's whole purpose."
        ),
        sql=_RESUMEN_ESTADOS_SQL,
        params=(_months_back_param(), _limit_param(10)),
        filter_metadata=_all_statuses_metadata,
    ),
}
