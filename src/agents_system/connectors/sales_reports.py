"""Portable sales report catalog, over a fixed view contract.

This replaces a catalog written directly against one company's own tables —
`orders`, `order_items`, `clients`. That version worked and was unusable
anywhere else, because the next company's sales table is similar but not
identical: different table names, different column names, and a different
status vocabulary (`facturada` where this one says `confirmed`). It was
deleted with the rest of that client's domain (issue #70).

Rewriting the SQL per client does not scale, and generating it from a
table/column mapping means splicing identifiers into SQL text — which is the
one thing `services/reports.py` is built never to do.

So the schema difference is absorbed OUTSIDE the SQL, in four read-only views
each deployment creates over whatever its own tables are called:

    agents_system_customers   customer_id, name, zone, segment
    agents_system_sales       sale_id, sold_at, customer_id, status, amount
    agents_system_sale_items  sale_id, sku, description, quantity, amount
    agents_system_stock       sku, description, on_hand, reorder_point

The views normalize three things, not one:

1. **Names.** `facturas.fecha_emision` becomes `agents_system_sales.sold_at`.
2. **Vocabulary.** `status` MUST be one of `confirmed`, `pending`,
   `cancelled`. A deployment whose database says `facturada` maps it in the
   view; every report's status filter and every `statuses_included`
   disclosure depends on that being true.
3. **Grain.** One row per sale in `agents_system_sales`, one row per line in
   `agents_system_sale_items`. A schema that stores sales at line grain
   aggregates in the view.

`demo/company/` is a worked example: a fake distributor whose tables are
called `facturas` / `factura_lineas` / `padron_clientes`, with Spanish columns
and its own status words, plus the views that map it onto this contract. Every
report in this module runs against it unchanged, which is the proof that the
contract carries the difference.

A deployment that cannot create views is not stuck: `run_report` takes any
`ReportSpec` catalog, so it supplies its own. That is the escape hatch the
deleted client-specific catalog used, and it remains open to any consumer.

These view names were `agentsys_*` before the package rename (issue #45,
`CHANGELOG.md`). A deployment that created its views under those names
before that rename runs `demo/MIGRATION-agentsys-rename.md` once.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Integer, bindparam, text

from agents_system.services.reports import ParamSpec, ReportSpec

# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: View name -> the columns a deployment must expose on it. Data rather than
#: prose so tests, the demo company, and any future contract-check command all
#: read the same source instead of drifting from a docstring.
CONTRACT_VIEWS: dict[str, tuple[str, ...]] = {
    "agents_system_customers": ("customer_id", "name", "zone", "segment"),
    "agents_system_sales": ("sale_id", "sold_at", "customer_id", "status", "amount"),
    "agents_system_sale_items": ("sale_id", "sku", "description", "quantity", "amount"),
    "agents_system_stock": ("sku", "description", "on_hand", "reorder_point"),
}

#: The canonical status vocabulary. A deployment's view maps its own words
#: onto these; nothing else in the platform knows what a sale status is.
ALL_STATUSES: tuple[str, ...] = ("confirmed", "pending", "cancelled")
NON_CANCELLED_STATUSES: tuple[str, ...] = ("confirmed", "pending")

#: What a view MUST emit for a source status it cannot map onto the vocabulary
#: above. Reserved, and deliberately not one of the three.
#:
#: The tempting answer is to fold unrecognized values into 'cancelled', since
#: that keeps them out of revenue. It is the wrong answer, because it is only
#: half a decision: `status_summary` does not filter by status, so those rows
#: come back counted as CANCELLATIONS the source never recorded. The agent then
#: reports a cancellation figure no row supports — a confident wrong number,
#: which is the failure this whole contract exists to make impossible.
#:
#: Outside the vocabulary, both halves stay honest: every status-filtered report
#: excludes these rows because they match none of the three, and
#: `status_summary` shows them under their own name, so the gap is visible to
#: whoever can fix the view.
UNMAPPED_STATUS: str = "unknown"

_STATUS_ALLOWED: tuple[str, ...] = ("default", "all", *ALL_STATUSES)

_DAYS_PER_MONTH = 30
"""Trailing window granularity for `months_back`: a coarse 30-day multiple,
not a calendar-month boundary. Good enough for a "last N months" analytical
window, and it avoids a calendar-arithmetic dependency. The approximation is
disclosed in every result's `meta` rather than left implicit."""


# ---------------------------------------------------------------------------
# Parameters, and what each result discloses about the filter it applied
# ---------------------------------------------------------------------------


def _resolve_statuses(status: str) -> tuple[str, ...]:
    """Map the caller-facing `status` value onto the statuses to filter by."""
    if status == "default":
        return NON_CANCELLED_STATUSES
    if status == "all":
        return ALL_STATUSES
    return (status,)


def _utcnow() -> datetime:
    """The single clock seam for this module.

    Exists so a test can count how many times a report run reads the clock.
    Patching `_months_back_to_since` cannot do that: `ParamSpec.transform`
    captures a direct reference at import time, so a monkeypatched module
    global is invisible to the bind path.
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

    `window_start` is read off the BOUND `since`, never recomputed: a second
    clock read would describe a window the query never used, and the point of
    this disclosure is that a reader can check the figure.
    """
    months_back = validated.get("months_back")
    since = bind_params.get("since")
    if months_back is None or since is None:
        return {}
    return {
        # No `int()` cast: `validate_params` already rejected any
        # `months_back` that is not an int (and rejects bool explicitly),
        # and the None case returned above. The cast could only ever be a
        # no-op, while reading as though this value were untrusted here.
        "window_days": _DAYS_PER_MONTH * months_back,
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
        description="How many trailing months of sales to include (1-24).",
        bind_as="since",
        transform=_months_back_to_since,
    )


def _limit_param(default: int) -> ParamSpec:
    # Deliberately no `maximum`: an over-large request should degrade to the
    # hard ceiling in `run_report.clamp_row_limit`, not bounce back a
    # validation error. `minimum=1` still rejects a genuine caller mistake.
    return ParamSpec(
        name="limit",
        type=int,
        default=default,
        minimum=1,
        description=(
            "Maximum rows to return (hard-capped at 500 regardless of this value)."
        ),
    )


def _status_param() -> ParamSpec:
    return ParamSpec(
        name="status",
        type=str,
        default="default",
        allowed=_STATUS_ALLOWED,
        description=(
            "Which sale statuses to count: 'default' (confirmed + pending, "
            "EXCLUDES cancelled), 'all' (every status, INCLUDES cancelled), "
            "or one specific status ('confirmed' / 'pending' / 'cancelled')."
        ),
        bind_as="statuses",
        transform=_resolve_statuses,
    )


# ---------------------------------------------------------------------------
# Static SQL. Every value is a named bindparam; `:statuses` is an expanding
# bindparam so a variable-length IN list stays a bound VALUE, never SQL text.
# Every relation is a contract view — enforced by
# tests/test_sales_reports_contract.py, not by discipline.
# ---------------------------------------------------------------------------

_SALES_BY_MONTH_SQL = text(
    """
    SELECT
        date_trunc('month', s.sold_at) AS month,
        COUNT(*) AS sale_count,
        COALESCE(SUM(s.amount), 0) AS revenue
    FROM agents_system_sales s
    WHERE s.status IN :statuses
      AND s.sold_at >= :since
    GROUP BY 1
    ORDER BY 1 ASC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_TOP_CUSTOMERS_SQL = text(
    """
    SELECT
        c.name AS customer_name,
        c.zone AS zone,
        COUNT(s.sale_id) AS sale_count,
        COALESCE(SUM(s.amount), 0) AS revenue
    FROM agents_system_customers c
    JOIN agents_system_sales s ON s.customer_id = c.customer_id
    WHERE s.status IN :statuses
      AND s.sold_at >= :since
    GROUP BY c.customer_id, c.name, c.zone
    ORDER BY revenue DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_SALES_BY_ZONE_SQL = text(
    """
    SELECT
        c.zone AS zone,
        COUNT(s.sale_id) AS sale_count,
        COALESCE(SUM(s.amount), 0) AS revenue,
        COALESCE(AVG(s.amount), 0) AS avg_ticket
    FROM agents_system_customers c
    JOIN agents_system_sales s ON s.customer_id = c.customer_id
    WHERE s.status IN :statuses
      AND s.sold_at >= :since
    GROUP BY c.zone
    ORDER BY revenue DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_SALES_BY_SEGMENT_SQL = text(
    """
    SELECT
        c.segment AS segment,
        COUNT(s.sale_id) AS sale_count,
        COALESCE(SUM(s.amount), 0) AS revenue,
        COALESCE(AVG(s.amount), 0) AS avg_ticket
    FROM agents_system_customers c
    JOIN agents_system_sales s ON s.customer_id = c.customer_id
    WHERE s.status IN :statuses
      AND s.sold_at >= :since
    GROUP BY c.segment
    ORDER BY revenue DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_TOP_PRODUCTS_SQL = text(
    """
    SELECT
        i.sku AS sku,
        MAX(i.description) AS description,
        SUM(i.quantity) AS total_quantity,
        COALESCE(SUM(i.amount), 0) AS revenue
    FROM agents_system_sale_items i
    JOIN agents_system_sales s ON s.sale_id = i.sale_id
    WHERE s.status IN :statuses
      AND s.sold_at >= :since
    GROUP BY i.sku
    ORDER BY revenue DESC, sku ASC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

#: Same rows, ranking, and truncation point as `_TOP_PRODUCTS_SQL` - only the
#: `ORDER BY` column differs. Issue #44: `run_report` used to always execute
#: `_TOP_PRODUCTS_SQL`, so a caller asking for "top products by units" got the
#: revenue-ranked top-N already truncated by `LIMIT`, then re-sorted it by
#: `total_quantity` client-side - a high-volume, low-price product outside
#: that revenue-ranked top-N could never appear, no matter how many units it
#: sold. This variant does the units ranking, and the truncation, IN the same
#: query, so `LIMIT` can never discard a row before the requested ordering
#: has seen it. Selected only via `ReportSpec.order_by_sql` - a closed,
#: pre-built statement, never a caller-composed one (AD-2).
_TOP_PRODUCTS_BY_UNITS_SQL = text(
    """
    SELECT
        i.sku AS sku,
        MAX(i.description) AS description,
        SUM(i.quantity) AS total_quantity,
        COALESCE(SUM(i.amount), 0) AS revenue
    FROM agents_system_sale_items i
    JOIN agents_system_sales s ON s.sale_id = i.sale_id
    WHERE s.status IN :statuses
      AND s.sold_at >= :since
    GROUP BY i.sku
    ORDER BY total_quantity DESC, sku ASC
    LIMIT :limit
    """
).bindparams(
    bindparam("statuses", expanding=True),
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_STATUS_SUMMARY_SQL = text(
    """
    SELECT
        s.status AS status,
        COUNT(*) AS sale_count,
        COALESCE(SUM(s.amount), 0) AS revenue
    FROM agents_system_sales s
    WHERE s.sold_at >= :since
    GROUP BY s.status
    ORDER BY sale_count DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("since", type_=DateTime(timezone=True)),
    bindparam("limit", type_=Integer()),
)

_LOW_STOCK_SQL = text(
    """
    SELECT
        st.sku AS sku,
        st.description AS description,
        st.on_hand AS on_hand,
        st.reorder_point AS reorder_point,
        st.reorder_point - st.on_hand AS shortfall
    FROM agents_system_stock st
    WHERE st.on_hand <= st.reorder_point * :threshold_ratio
    ORDER BY shortfall DESC
    LIMIT :limit
    """
).bindparams(
    bindparam("threshold_ratio", type_=Integer()),
    bindparam("limit", type_=Integer()),
)


def _order_by_param() -> ParamSpec:
    return ParamSpec(
        name="order_by",
        type=str,
        default="revenue",
        allowed=("revenue", "units"),
        description=(
            "Ranking applied BEFORE truncating to `limit`: 'revenue' "
            "(default, backward compatible) or 'units' (total quantity "
            "sold). Use 'units' for a \"top products by units/volume\" "
            "question - re-sorting the default revenue-ranked rows "
            "client-side is wrong, because a high-volume, low-price "
            "product outside the revenue top-N is never in those rows to "
            "begin with."
        ),
    )


def _threshold_ratio_param() -> ParamSpec:
    return ParamSpec(
        name="threshold_ratio",
        type=int,
        default=1,
        minimum=1,
        maximum=5,
        description=(
            "How far above the reorder point still counts as low: 1 means at "
            "or below the reorder point, 2 means at or below twice it."
        ),
    )


def _stock_metadata(
    validated: Mapping[str, Any], _bind_params: Mapping[str, Any]
) -> dict[str, Any]:
    """Stock is a point-in-time reading, and a reader cannot tell that from
    the rows. Saying so stops the agent presenting it as a period figure."""
    return {
        "threshold_ratio": validated.get("threshold_ratio"),
        "reading": "current on-hand at query time - not a period aggregate.",
    }


CATALOG: dict[str, ReportSpec] = {
    "sales_by_month": ReportSpec(
        name="sales_by_month",
        description=(
            "Monthly sale count and revenue over a trailing window. Use for "
            "trend questions such as how sales moved month to month."
        ),
        sql=_SALES_BY_MONTH_SQL,
        params=(_months_back_param(), _limit_param(24), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "top_customers": ReportSpec(
        name="top_customers",
        description=(
            "Highest-revenue customers over a trailing window, with their "
            "zone and sale count. Use for 'who are our biggest buyers'."
        ),
        sql=_TOP_CUSTOMERS_SQL,
        params=(_months_back_param(), _limit_param(10), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "sales_by_zone": ReportSpec(
        name="sales_by_zone",
        description=(
            "Revenue, sale count and average ticket grouped by customer zone "
            "over a trailing window. Use for geographic comparisons."
        ),
        sql=_SALES_BY_ZONE_SQL,
        params=(_months_back_param(), _limit_param(20), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "sales_by_segment": ReportSpec(
        name="sales_by_segment",
        description=(
            "Revenue, sale count and average ticket grouped by customer "
            "segment over a trailing window. Use to compare kinds of "
            "customer rather than places."
        ),
        sql=_SALES_BY_SEGMENT_SQL,
        params=(_months_back_param(), _limit_param(20), _status_param()),
        filter_metadata=_status_filter_metadata,
    ),
    "top_products": ReportSpec(
        name="top_products",
        description=(
            "Best-selling products over a trailing window, with revenue and "
            "units sold. Ranked by revenue by default; pass "
            "order_by='units' to rank by units sold instead - do not "
            "re-sort the default (revenue) rows client-side for a 'top by "
            "units' question, they are already truncated to the wrong top-N. "
            "Use for 'what sells most' (revenue) or 'what sells the most "
            "units/volume' (order_by='units')."
        ),
        sql=_TOP_PRODUCTS_SQL,
        params=(
            _months_back_param(),
            _limit_param(10),
            _status_param(),
            _order_by_param(),
        ),
        filter_metadata=_status_filter_metadata,
        order_by_param="order_by",
        order_by_sql={"units": _TOP_PRODUCTS_BY_UNITS_SQL},
    ),
    "status_summary": ReportSpec(
        name="status_summary",
        description=(
            "Sale count and revenue broken down BY status over a trailing "
            "window. Unlike the other reports this one is not filtered by "
            "status - it is how you see cancellations."
        ),
        sql=_STATUS_SUMMARY_SQL,
        params=(_months_back_param(), _limit_param(10)),
        filter_metadata=_all_statuses_metadata,
    ),
    "low_stock": ReportSpec(
        name="low_stock",
        description=(
            "Products at or below their reorder point, worst shortfall "
            "first. A current reading, not a period aggregate."
        ),
        sql=_LOW_STOCK_SQL,
        params=(_threshold_ratio_param(), _limit_param(25)),
        filter_metadata=_stock_metadata,
    ),
}
