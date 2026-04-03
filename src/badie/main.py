from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from badie.config import Settings, get_settings
from badie.services.redis import close_redis_pool


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup / shutdown resources."""
    # Startup — pool is created lazily on first use
    yield
    # Shutdown — release Redis connections
    await close_redis_pool()


def create_app() -> FastAPI:
    """Application factory. Returns configured FastAPI instance."""
    settings = get_settings()
    application = FastAPI(
        title="Badie",
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )

    @application.get("/health")
    async def health(
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment}

    return application


app = create_app()
