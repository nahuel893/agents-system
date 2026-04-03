from fastapi import Depends, FastAPI

from badie.config import Settings, get_settings


def create_app() -> FastAPI:
    """Application factory. Returns configured FastAPI instance.

    Routes, middleware, and lifespan added in later pasos.
    """
    settings = get_settings()
    application = FastAPI(
        title="Badie",
        version="0.1.0",
        debug=settings.debug,
    )

    @application.get("/health")
    async def health(
        settings: Settings = Depends(get_settings),
    ) -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment}

    return application


app = create_app()
