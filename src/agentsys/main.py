"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from fastapi import Depends, FastAPI, Request
from sqlalchemy import text

from agentsys.config import Settings, get_settings
from agentsys.integration import webhook_router
from agentsys.models.base import get_engine
from agentsys.observability import RequestIdMiddleware, setup_logging
from agentsys.services.redis import close_redis_pool, get_redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup / shutdown resources."""
    settings = get_settings()
    # Startup — create async engine and store on app state
    app.state.engine = get_engine(settings.database_url)
    yield
    # Shutdown — dispose engine and release Redis connections
    await app.state.engine.dispose()
    await close_redis_pool()


def create_app() -> FastAPI:
    """Application factory. Returns configured FastAPI instance."""
    setup_logging()

    settings = get_settings()
    application = FastAPI(
        title="Badie",
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )

    application.add_middleware(RequestIdMiddleware)
    application.include_router(webhook_router)

    logger = structlog.get_logger()

    @application.get("/health")
    async def health(
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        engine = request.app.state.engine
        redis_client = get_redis_client(settings.redis_url)

        postgres_status: Literal["ok", "error"] = "ok"
        redis_status: Literal["ok", "error"] = "ok"

        # Probe PostgreSQL
        try:
            async with asyncio.timeout(3.0):
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
        except Exception:
            logger.warning("health.postgres_error")
            postgres_status = "error"

        # Probe Redis
        try:
            async with asyncio.timeout(3.0):
                await redis_client.ping()
        except Exception:
            logger.warning("health.redis_error")
            redis_status = "error"

        overall: Literal["ok", "degraded"] = (
            "ok" if postgres_status == "ok" and redis_status == "ok" else "degraded"
        )

        return {
            "status": overall,
            "environment": settings.environment,
            "postgres": postgres_status,
            "redis": redis_status,
        }

    return application


app = create_app()
