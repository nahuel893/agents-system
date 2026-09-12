"""Load the fake company and check it satisfies the report contract.

    uv run python demo/load_demo_company.py

Reads `DEMO_DATABASE_URL`, falling back to a local `agentsys_demo` database on
the compose Postgres. The database must already exist:

    docker exec agents-system-postgres-1 psql -U postgres -c 'CREATE DATABASE agentsys_demo;'

Loading is destructive and deliberately so — the schema file drops its own
tables first, so a reload is the way to get back to a known state. It refuses
to run against a database holding tables it does not recognise, because the
obvious accident here is pointing it at a real one.

The contract check is the part worth having. `02_views.sql` claims to expose
the columns `sales_reports.CONTRACT_VIEWS` requires; this asks the live
database instead of believing the comment, so a renamed column fails here
rather than four layers up as an empty report.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentsys.connectors.sales_reports import (  # noqa: E402
    CONTRACT_VIEWS,
    UNMAPPED_STATUS,
)

_DEFAULT_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agentsys_demo"

_SQL_FILES = ("01_schema.sql", "02_views.sql", "03_seed.sql")

DEMO_MARKER_TABLE = "agentsys_demo_marker"
"""The loader's own signature, created by `01_schema.sql`.

This exists because the previous guard asked the wrong question. It listed the
tables it found, subtracted the five this demo creates, and loaded when the
remainder was empty — so it was testing whether anything looked FOREIGN, and
concluded safety from the absence of surprise.

That fails precisely where it matters. This demo is named like a real Argentine
distributor's ERP on purpose (`facturas`, `factura_lineas`, `articulos`), so a
real such database holding `facturas` and `articulos` produced an empty
remainder and passed — and `01_schema.sql` opens with `DROP TABLE IF EXISTS
facturas`. The databases most likely to be hit by a mistyped
`DEMO_DATABASE_URL` were exactly the ones the check waved through.

A name proves nothing about who owns a database. A marker this loader wrote
does, so that is the only positive evidence accepted.
"""


def classify_target(*, tables: set[str], views: set[str]) -> str | None:
    """Decide whether this database may be overwritten.

    Returns `None` when loading is safe, or the reason to refuse.

    Safe in exactly two cases: the database is completely empty (a virgin
    scratch database, the documented first load), or it carries
    `DEMO_MARKER_TABLE` (this loader wrote it, so a reload destroys only the
    loader's own deterministic data).

    Everything else is refused. There is deliberately no override flag: the
    remedy is to create a fresh database, which costs one command, and an
    override would be reached for in exactly the situation the guard exists to
    stop.

    Pure, and separate from the queries that feed it, so the decision is
    testable without a Postgres holding a real company's data.
    """
    if DEMO_MARKER_TABLE in tables:
        return None
    if not tables and not views:
        return None

    found = ", ".join(sorted(tables | views))
    return (
        f"this database already holds objects and does not carry the "
        f"'{DEMO_MARKER_TABLE}' table, so it was not created by this loader: "
        f"{found}. Loading would DROP tables. If this is a scratch database, "
        f"drop it and create an empty one; if you recognise those names as "
        f"real data, the URL is pointing at the wrong database."
    )


async def _existing_tables(conn: AsyncConnection) -> set[str]:
    result = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    )
    return {row[0] for row in result}


async def _existing_views(conn: AsyncConnection) -> set[str]:
    """Views matter too.

    The old check asked only for BASE TABLEs, so a database whose tables live in
    another schema and whose `public` holds only views read as empty and was
    loaded over.
    """
    result = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.views "
            "WHERE table_schema = 'public'"
        )
    )
    return {row[0] for row in result}


async def _view_columns(conn: AsyncConnection, view: str) -> set[str]:
    result = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :view"
        ),
        {"view": view},
    )
    return {row[0] for row in result}


async def _run_sql_files(conn: AsyncConnection, base: Path) -> None:
    # Straight down to the asyncpg connection. Every SQLAlchemy execute path
    # sends a PREPARED statement, and Postgres rejects multiple commands in
    # one of those ("cannot insert multiple commands into a prepared
    # statement") — which is what a .sql file is. asyncpg's own `execute`
    # with no arguments uses the simple query protocol, which accepts them.
    #
    # The alternative, splitting the file on ';', breaks the first time a
    # statement contains a semicolon inside a string literal or a function
    # body. Not worth the trap for a loader.
    raw = await conn.get_raw_connection()
    driver_conn = raw.driver_connection
    if driver_conn is None:
        # `driver_connection` is typed optional and a pooled connection can be
        # detached. Falling through would raise AttributeError from inside the
        # load, after the guard already cleared the target and with an unknown
        # number of the three files applied. A loader that half-ran is worse
        # than one that did not start, so this stops before the first DROP.
        raise RuntimeError(
            "no raw asyncpg connection available, so the SQL files were not "
            "run and the database was not modified"
        )
    for name in _SQL_FILES:
        await driver_conn.execute((base / name).read_text())
        print(f"  ran {name}")


async def _unmapped_statuses(conn: AsyncConnection) -> list[tuple[str, int]]:
    """Source statuses the view could not map, with how many rows each covers.

    The view sends anything it does not recognise to `UNMAPPED_STATUS` instead of
    guessing a canonical one, which keeps those rows out of every revenue figure
    and visible in `status_summary`. That makes the problem discoverable; this
    makes it impossible to miss, by failing the load that introduced it.

    Reads the source column, not the view, so the message can name the actual
    word somebody has to add to the CASE.
    """
    result = await conn.execute(
        text(
            "SELECT f.estado, COUNT(*) FROM facturas f "
            "JOIN agentsys_sales s ON s.sale_id = f.nro_factura "
            "WHERE s.status = :unmapped "
            "GROUP BY f.estado ORDER BY f.estado"
        ),
        {"unmapped": UNMAPPED_STATUS},
    )
    return [(row[0], row[1]) for row in result]


async def _verify_contract(conn: AsyncConnection) -> list[str]:
    problems: list[str] = []
    for view, required in CONTRACT_VIEWS.items():
        present = await _view_columns(conn, view)
        if not present:
            problems.append(f"view '{view}' does not exist")
            continue
        missing = sorted(set(required) - present)
        if missing:
            problems.append(f"view '{view}' is missing columns: {missing}")

    # Only meaningful once agentsys_sales exists; a missing view is already
    # reported above and this query would fail rather than add information.
    if not problems:
        for estado, count in await _unmapped_statuses(conn):
            problems.append(
                f"{count} sale(s) carry source status '{estado}', which "
                f"02_views.sql does not map — they are reported as "
                f"'{UNMAPPED_STATUS}' and excluded from every revenue figure. "
                f"Add it to the CASE in agentsys_sales."
            )
    return problems


async def _summarize(conn: AsyncConnection) -> None:
    for view in CONTRACT_VIEWS:
        # The only interpolated identifier in this file. `view` comes from the
        # platform's own CONTRACT_VIEWS keys, never from input — but "it comes
        # from a constant" is an invariant nothing checks, and this file's other
        # job is refusing to trust what it was handed. So verify it instead of
        # asserting it: a future contract key that is not a plain identifier
        # stops here rather than reaching the database as SQL.
        if not view.isidentifier():
            raise ValueError(f"refusing to interpolate non-identifier view name: {view!r}")
        result = await conn.execute(text(f"SELECT COUNT(*) FROM {view}"))  # noqa: S608
        print(f"  {view:<22} {result.scalar_one():>6} rows")


async def main() -> int:
    url = os.getenv("DEMO_DATABASE_URL", _DEFAULT_URL)
    base = Path(__file__).resolve().parent / "company"
    engine = create_async_engine(url, echo=False)

    try:
        async with engine.begin() as conn:
            refusal = classify_target(
                tables=await _existing_tables(conn),
                views=await _existing_views(conn),
            )
            if refusal is not None:
                print(f"refusing to load: {refusal}", file=sys.stderr)
                return 1

            print("loading the demo company...")
            await _run_sql_files(conn, base)

        async with engine.connect() as conn:
            problems = await _verify_contract(conn)
            if problems:
                print("\ncontract NOT satisfied:", file=sys.stderr)
                for problem in problems:
                    print(f"  - {problem}", file=sys.stderr)
                return 1

            print("\ncontract satisfied:")
            await _summarize(conn)
    finally:
        await engine.dispose()

    print("\nready. Point BI_DATABASE_URL at this database to run the reports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
