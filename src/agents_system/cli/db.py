"""``agents db upgrade`` and ``agents db current``: the platform's migrations.

The migrations ship inside the package (``agents_system/migrations``), so an
installed wheel can apply them with no checkout and no ``alembic.ini``
(issue #312). The database URL is resolved by ``migrations/env.py``:
``DATABASE_URL``, which ``--database-url`` sets for this command, else the
served app's configured one.
"""

from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sys
from typing import Any, TextIO

from agents_system.extras import raise_missing_extra

#: Where the packaged migrations are.
MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"


def add_parser(subparsers: Any) -> None:
    parser = subparsers.add_parser(
        "db",
        help="apply the platform's database migrations (needs the postgres extra)",
        description="The platform's database migrations, shipped with the package.",
    )
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)
    upgrade = actions.add_parser("upgrade", help="apply every pending migration")
    current = actions.add_parser("current", help="print the applied revision")
    for action in (upgrade, current):
        action.add_argument(
            "--database-url",
            metavar="URL",
            help=(
                "the database to migrate (default: DATABASE_URL, else the "
                "served app's configured database)"
            ),
        )
        action.set_defaults(run=run)


def alembic_config() -> Any:
    """An alembic ``Config`` for the packaged migrations, with no ini file."""
    try:
        import sqlalchemy  # noqa: F401 -- `migrations/env.py` needs it
        from alembic.config import Config
    except ImportError as error:
        raise_missing_extra("postgres", "agents db", error)
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


def run(args: argparse.Namespace, out: TextIO) -> int:
    config = alembic_config()
    from alembic import command

    if args.database_url:
        os.environ["DATABASE_URL"] = args.database_url
    if args.action == "upgrade":
        # alembic reports each migration it applies on its own logger.
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        alembic_logger = logging.getLogger("alembic")
        alembic_logger.addHandler(handler)
        alembic_logger.setLevel(logging.INFO)
        try:
            command.upgrade(config, "head")
        finally:
            alembic_logger.removeHandler(handler)
        out.write("The database is at the latest revision.\n")
        return 0
    command.current(config)
    return 0
