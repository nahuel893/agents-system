"""Serve the demo database through the OpenAI-compatible adapter.

Run ``uv run python -m agents_system.demo`` after loading the demo database
with ``demo/load_demo_company.py``; this module is that loader's "serve it"
half. Configuration is environment-variable only.

Set ``AGENT_REGISTRATIONS``, ``ADAPTER_RUNTIMES`` and ``ADAPTER_API_KEY`` to
publish a role on ``/v1/*``; this module deliberately sets none of them. Boot
also requires an explicit ``DEPLOY_GRANTS`` entry for every registered
runtime id (issue #38) -- this module does not set that either. See
``docs/platform/demo-entrypoint.md`` for the exact command.

Two things ``main()`` refuses to boot without, both enforced fail-closed unless
you explicitly opt out with ``ALLOW_INSECURE=true``:

- ``create_app``'s lifespan runs ``Settings.validate_security_fail_closed`` at
  startup, which raises if ``META_WEBHOOK_SECRET`` is empty (an empty HMAC key
  makes webhook signatures forgeable). Set ``META_WEBHOOK_SECRET`` to any
  non-empty value for a local demo run — it does not need to be a real Meta
  secret, since this entrypoint never receives WhatsApp webhooks.
- ``main()`` also verifies, before serving anything, that the role behind
  ``DEMO_DATABASE_URL`` actually has ``default_transaction_read_only = on``
  (see :func:`_ensure_read_only_engine`). A role that can write is refused.

``ALLOW_INSECURE=true`` bypasses both checks, but it is the wider hammer: it
*also* allows ``ADAPTER_RUNTIMES`` to be configured without ``ADAPTER_API_KEY``
(an open, unauthenticated ``/v1/*``). Prefer setting ``META_WEBHOOK_SECRET``
plus a genuinely read-only ``DEMO_DATABASE_URL`` role for a demo run, and keep
``ALLOW_INSECURE=true`` as the local-dev fallback when that is inconvenient.
See ``docs/platform/demo-entrypoint.md`` for the exact commands.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog
from fastapi import FastAPI
from langchain_core.language_models import BaseChatModel
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agents_system.config import get_settings
from agents_system.evals.live_registry import build_live_registry_factory
from agents_system.evals.provider import build_eval_model
from agents_system.harness.registry import RegistryFactory, ToolRegistry
from agents_system.main import create_app
from agents_system.services.db_role import role_is_read_only

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


async def _ensure_read_only_engine(
    engine: AsyncEngine, *, allow_insecure: bool
) -> None:
    """Refuse to serve the demo database unless its role is read-only.

    Reuses the shared :func:`agents_system.services.db_role.role_is_read_only`
    check -- the same one ``main.py``'s lifespan runs for `BI_DATABASE_URL`
    before binding `run_report` -- so `DEMO_DATABASE_URL` gets the identical
    guarantee: a role someone configured by hand is verified, not trusted.

    - Confirmed NOT read-only (``False``) and *allow_insecure* is False:
      raises :class:`SystemExit` with an actionable message. This is the
      default, fail-closed path -- the demo entrypoint has no business
      writing to its own reference data.
    - Confirmed NOT read-only and *allow_insecure* is True: logs a loud
      warning and returns. The caller explicitly asked to bypass this, but a
      silent bypass would defeat the point of checking at all.
    - Undetermined (``None``, e.g. the database did not answer): logs a
      warning and returns, mirroring `main.py`'s own "unverified, proceed"
      treatment for `BI_DATABASE_URL` -- an unreachable database must not
      read as "confirmed writable" and block an otherwise-fine demo run.
    - Confirmed read-only (``True``): returns normally.
    """
    logger = structlog.get_logger()
    read_only = await role_is_read_only(engine, log_event="demo.read_only_check_failed")

    if read_only is False:
        if allow_insecure:
            logger.error(
                "demo.read_only_check_failed_but_continuing",
                message=(
                    "DEMO_DATABASE_URL points at a role with "
                    "default_transaction_read_only = off, but continuing "
                    "because ALLOW_INSECURE=true. Do NOT do this against a "
                    "database you care about -- the agent can write to it."
                ),
            )
            return
        raise SystemExit(
            "DEMO_DATABASE_URL points at a role with "
            "default_transaction_read_only = off. Create a dedicated "
            "read-only role for the demo (see "
            "docs/platform/demo-entrypoint.md) or set ALLOW_INSECURE=true "
            "to bypass this for local dev only."
        )

    if read_only is None:
        logger.warning(
            "demo.read_only_unverified",
            message=(
                "Could not verify that DEMO_DATABASE_URL is read-only -- "
                "the database did not answer. Serving it anyway."
            ),
        )
        return

    logger.info("demo.read_only_verified")


def main() -> None:
    """Build and run the configured demo API server."""
    database_url = os.getenv("DEMO_DATABASE_URL", _DEFAULT_DEMO_DATABASE_URL)
    engine = create_async_engine(database_url)

    settings = get_settings()
    asyncio.run(
        _ensure_read_only_engine(engine, allow_insecure=settings.allow_insecure)
    )

    model, _model_name = build_eval_model()
    app = build_app(engine=engine, model=model)

    host = os.getenv("DEMO_HOST", _DEFAULT_HOST)
    port = int(os.getenv("DEMO_PORT", str(_DEFAULT_PORT)))

    import uvicorn

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
