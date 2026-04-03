from fastapi import FastAPI


def create_app() -> FastAPI:
    """Application factory. Returns bare FastAPI instance.

    Routes, middleware, and lifespan added in later pasos.
    """
    application = FastAPI(title="Badie", version="0.1.0")

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
