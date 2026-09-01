"""Read-only async engine for the medallion data warehouse.

The medallion warehouse is the source of truth for `gold.dim_articulo` and
`gold.dim_cliente`. The bot only READS from it via dedicated sync pipelines
(``sync_articles``, ``sync_clients``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import model_validator
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentsys.config import Settings


def get_medallion_engine(url: str) -> AsyncEngine:
    """Create an async engine for the medallion warehouse.

    Use ``execution_options(readonly=True)`` at the session/connection level
    to enforce read-only at the application layer.
    """
    return create_async_engine(url, echo=False, pool_pre_ping=True)


class MedallionSettings(Settings):
    """ACME's settings, extending the platform surface with its warehouse.

    The worked example of the extension point: `agentsys` owns no second
    database, so these fields and the validator that composes their URL live
    with the deployment that has one. pydantic-settings reads a subclass's
    fields from the same environment, and the platform validators still run.

    Every override falls back to the main DB connection — same server,
    different database — so only the parts that differ need setting.
    """

    medallion_db_user: str | None = None
    medallion_db_password: str | None = None
    medallion_db_host: str | None = None
    medallion_db_port: int | None = None
    medallion_db_name: str = "medallion"
    medallion_database_url: str = "postgresql+asyncpg://localhost:5432/medallion"

    @model_validator(mode="after")
    def compose_medallion_url(self) -> "MedallionSettings":
        if self.db_user is None and self.db_host is None:
            return self

        self.medallion_database_url = URL.create(
            "postgresql+asyncpg",
            username=self.medallion_db_user or self.db_user,
            password=self.medallion_db_password or self.db_password,
            host=self.medallion_db_host or self.db_host,
            port=self.medallion_db_port or self.db_port,
            database=self.medallion_db_name,
        ).render_as_string(hide_password=False)

        return self


@lru_cache
def get_medallion_settings() -> MedallionSettings:
    """Cached singleton of this deployment's settings.

    The client-side counterpart of `config.get_settings`. Scripts that read
    warehouse connection values must use this: `get_settings()` returns the
    platform surface, which deliberately has no `medallion_*` fields.
    """
    return MedallionSettings()
