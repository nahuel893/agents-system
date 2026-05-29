"""Read-only async engine for the medallion data warehouse.

The medallion warehouse is the source of truth for `gold.dim_articulo` and
`gold.dim_cliente`. The bot only READS from it via dedicated sync pipelines
(``sync_articles``, ``sync_clients``).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def get_medallion_engine(url: str) -> AsyncEngine:
    """Create an async engine for the medallion warehouse.

    Use ``execution_options(readonly=True)`` at the session/connection level
    to enforce read-only at the application layer.
    """
    return create_async_engine(url, echo=False, pool_pre_ping=True)
