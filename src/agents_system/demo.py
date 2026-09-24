"""Serve the demo database through the OpenAI-compatible adapter.

Run ``uv run python -m agents_system.demo`` after loading the demo database
with ``demo/load_demo_company.py``; this module is that loader's "serve it"
half. Configuration is environment-variable only.

Set ``ADAPTER_RUNTIMES`` and ``ADAPTER_API_KEY`` to publish a role on ``/v1/*``;
this module deliberately does not set either value. The in-progress permission
model will additionally require explicit ``DEPLOY_GRANTS`` once it lands; this
module does not anticipate that future configuration shape.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents_system.evals.live_registry import build_live_registry_factory
from agents_system.evals.provider import build_eval_model
from agents_system.harness.registry import RegistryFactory, ToolRegistry
from agents_system.main import create_app

_DEFAULT_DEMO_DATABASE_URL = (
    "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/agents_system_demo"
)
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 8000


def _demo_registry_factory(
    engine: AsyncEngine, model: BaseChatModel
) -> RegistryFactory:
    """Adapt the demo's zero-argument registry builder for ``create_app``."""
    build_registry = build_live_registry_factory(engine, model)

    def factory(
        settings: Any, embedder: Any = None, bi_engine: Any = None
    ) -> ToolRegistry:
        return build_registry()

    return factory


def build_app(*, engine: AsyncEngine, model: BaseChatModel) -> FastAPI:
    """Build the demo API without reading environment variables."""
    return create_app(
        registry_factory=_demo_registry_factory(engine, model),
        title="agents_system demo",
    )


def main() -> None:
    """Build and run the configured demo API server."""
    database_url = os.getenv("DEMO_DATABASE_URL", _DEFAULT_DEMO_DATABASE_URL)
    engine = create_async_engine(database_url)
    model, _model_name = build_eval_model()
    app = build_app(engine=engine, model=model)

    host = os.getenv("DEMO_HOST", _DEFAULT_HOST)
    port = int(os.getenv("DEMO_PORT", str(_DEFAULT_PORT)))

    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
