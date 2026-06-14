"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

import structlog
from fastapi import Depends, FastAPI, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentsys.config import Settings, get_settings
from agentsys.integration import openai_router, webhook_router
from agentsys.models.base import get_engine
from agentsys.observability import RequestIdMiddleware, setup_logging
from agentsys.services.redis import close_redis_pool, get_redis_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup / shutdown resources."""
    settings = get_settings()

    # Startup — create async engine and store on app state
    app.state.engine = get_engine(settings.database_url)

    # D-012 — build runtime cache once at startup.
    # Imports are deferred to avoid loading heavy dependencies (torch, sentence-
    # transformers) when they are not needed (e.g. during testing with mocked state).
    if settings.adapter_runtimes:
        _logger = structlog.get_logger()
        if not settings.adapter_api_key:
            _logger.warning(
                "adapter.open_mode",
                message=(
                    "ADAPTER_API_KEY is not set. "
                    "The /v1/* endpoints are open — set a key in production."
                ),
            )

        from agentsys.agent.graph import AgentRuntime
        from agentsys.connectors.rag_connector import build_badie_rag_registry
        from agentsys.harness.factory import build_runtime
        from agentsys.services.embeddings import get_embedding_provider

        embedder = get_embedding_provider(settings)
        registry = build_badie_rag_registry(settings, embedder)
        session_provider = async_sessionmaker(
            app.state.engine, expire_on_commit=False
        )

        # Build chat model from configured provider
        model = _build_chat_model(settings.adapter_provider)

        runtimes: dict[str, AgentRuntime] = {}
        for model_id in settings.adapter_runtimes:
            if "__" not in model_id:
                _logger.error(
                    "adapter.invalid_runtime_id",
                    model_id=model_id,
                    reason="Expected '{deployment}__{role}' format",
                )
                continue
            prefix, role = model_id.split("__", 1)
            deployment: str | None = None if prefix == "_generic" else prefix
            equipped = build_runtime(
                role_type=role,
                registry=registry,
                granted_permissions=_runtime_permissions(role),
                client=deployment,
                session_provider=session_provider,
            )
            runtimes[model_id] = AgentRuntime(runtime=equipped, model=model)
            _logger.info("adapter.runtime_cached", model_id=model_id)

        app.state.runtimes = runtimes
    else:
        app.state.runtimes = {}

    yield

    # Shutdown — dispose engine and release Redis connections
    await app.state.engine.dispose()
    await close_redis_pool()


def _build_chat_model(provider: str) -> Any:  # noqa: ANN401
    """Construct the chat model for the configured adapter provider.

    Returns a LangChain BaseChatModel instance for the configured provider.
    Return typed as Any to avoid mypy false-positives from lazy imports.
    """
    import os

    if provider == "groq":
        from langchain_groq import ChatGroq

        api_key: str | None = os.getenv("GROQ_API_KEY") or None
        return ChatGroq(model="llama-3.3-70b-versatile", api_key=api_key)  # type: ignore[arg-type]

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY") or None
        return ChatAnthropic(  # type: ignore[call-arg]
            model="claude-3-5-haiku-latest",
            anthropic_api_key=anthropic_api_key,
        )

    # Default: ollama
    from langchain_ollama import ChatOllama

    return ChatOllama(model="qwen2.5:3b", temperature=0)


def _runtime_permissions(role: str) -> list[str]:
    """Return the granted permissions for a runtime by role name.

    This is a thin mapping — RBAC details live in the harness definitions.
    The sales-agent gets the full BADIE tool surface.
    """
    _sales_permissions = [
        "read:catalog",
        "read:client_registry",
        "write:orders",
        "write:order_items",
        "send:message",
    ]
    if role == "sales-agent":
        return _sales_permissions
    return []


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
    application.include_router(openai_router)

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
