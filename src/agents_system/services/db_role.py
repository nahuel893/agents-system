"""Shared read-only-role verification.

Extracted from ``main._bi_role_is_read_only`` so more than one entrypoint can
ask the same question of a database role without duplicating the reasoning:
``main.py``'s lifespan asks it about ``BI_DATABASE_URL`` before binding
``run_report``, and ``demo.py``'s entrypoint asks it about
``DEMO_DATABASE_URL`` before serving the demo database at all.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError


async def role_is_read_only(
    engine: Any, *, log_event: str = "db.read_only_check_failed"
) -> bool | None:
    """Ask the database whether the role behind *engine* really is read-only.

    Returns True / False, or None when the question could not be answered —
    those are three different situations and collapsing the third into either
    of the other two is the bug. "Could not determine" must not read as
    "determined to be writable" (that would take a dependent feature down
    whenever a reporting replica is briefly unreachable), and it must not
    read as "determined to be read-only" either (that would restore the very
    assumption this check exists to remove).

    A dedicated read-only role is a layer that is meant to hold even if
    parameter validation and any application-level interceptor both have
    bugs, and nothing else confirms it was configured — the connection URL
    is trusted to point at a role someone set up by hand.

    *log_event* lets each caller keep its own existing log event name (e.g.
    ``main.py`` uses ``"bi.read_only_check_failed"``) rather than adopting a
    shared one that would change observable behaviour for callers this
    function did not originate from.
    """
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SHOW default_transaction_read_only"))
            return str(result.scalar()).strip().lower() == "on"
    except SQLAlchemyError:
        structlog.get_logger().warning(log_event, exc_info=True)
        return None
