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
from sqlalchemy.ext.asyncio import create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agentsys.connectors.sales_reports import CONTRACT_VIEWS  # noqa: E402

_DEFAULT_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agentsys_demo"

_SQL_FILES = ("01_schema.sql", "02_views.sql", "03_seed.sql")

#: Everything this demo owns. Anything else in `public` means the target is
#: not a scratch database and the load stops.
_OWNED_TABLES = {
    "padron_clientes",
    "articulos",
    "facturas",
    "factura_lineas",
    "existencias",
}


async def _existing_tables(conn) -> set[str]:
    result = await conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
    )
    return {row[0] for row in result}


async def _view_columns(conn, view: str) -> set[str]:
    result = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :view"
        ),
        {"view": view},
    )
    return {row[0] for row in result}


async def _run_sql_files(conn, base: Path) -> None:
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
    for name in _SQL_FILES:
        await driver_conn.execute((base / name).read_text())
        print(f"  ran {name}")


async def _verify_contract(conn) -> list[str]:
    problems: list[str] = []
    for view, required in CONTRACT_VIEWS.items():
        present = await _view_columns(conn, view)
        if not present:
            problems.append(f"view '{view}' does not exist")
            continue
        missing = sorted(set(required) - present)
        if missing:
            problems.append(f"view '{view}' is missing columns: {missing}")
    return problems


async def _summarize(conn) -> None:
    for view in CONTRACT_VIEWS:
        result = await conn.execute(text(f"SELECT COUNT(*) FROM {view}"))  # noqa: S608
        print(f"  {view:<22} {result.scalar_one():>6} rows")


async def main() -> int:
    url = os.getenv("DEMO_DATABASE_URL", _DEFAULT_URL)
    base = Path(__file__).resolve().parent / "company"
    engine = create_async_engine(url, echo=False)

    try:
        async with engine.begin() as conn:
            existing = await _existing_tables(conn)
            unknown = sorted(existing - _OWNED_TABLES)
            if unknown:
                print(
                    "refusing to load: this database holds tables the demo "
                    f"does not own: {unknown}",
                    file=sys.stderr,
                )
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
