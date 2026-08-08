"""FastAPI application factory."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Literal

import httpx
import structlog
from fastapi import Depends, FastAPI, Request
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agentsys.config import Settings, get_settings
from agentsys.integration import openai_router, webhook_router
from agentsys.integration.whatsapp_client import WhatsAppClient
from agentsys.models.base import get_engine
from agentsys.observability import RequestIdMiddleware, setup_logging
from agentsys.services.redis import close_redis_pool, get_redis_client


def _checkpointer_ttl_config(checkpointer_ttl_s: int | None) -> dict[str, Any] | None:
    """Convert AD-7's ``checkpointer_ttl_s`` (seconds) into the
    langgraph-checkpoint-redis package's ``ttl`` dict shape, whose
    ``default_ttl`` is expressed in MINUTES."""
    if checkpointer_ttl_s is None:
        return None
    return {"default_ttl": checkpointer_ttl_s / 60, "refresh_on_read": True}


def _build_checkpointer_cm(settings: Settings) -> Any:  # noqa: ANN401
    """Build the ``AsyncRedisSaver`` async context manager (design AD-1/AD-7).

    Deferred import — the checkpoint-redis package is only needed when there
    is at least one runtime to inject it into. Builds its OWN Redis
    connection from ``redis_url``: the shared pool in ``services/redis.py``
    uses ``decode_responses=True``, which corrupts binary checkpoint
    payloads — do NOT reuse ``get_redis_client`` here.

    ``AsyncRedisSaver.from_conn_string(...)`` (confirmed against the
    installed ``langgraph-checkpoint-redis==0.3.6``) is itself an
    ``@asynccontextmanager`` classmethod: entering it constructs the saver
    and calls ``asetup()`` (idempotent index creation) + ``aset_client_info()``
    via ``__aenter__``; it must be held open for the app's lifetime and torn
    down via its ``__aexit__`` at shutdown — the caller does this through the
    lifespan's ``AsyncExitStack``.
    """
    from langgraph.checkpoint.redis import AsyncRedisSaver

    return AsyncRedisSaver.from_conn_string(
        settings.redis_url,
        ttl=_checkpointer_ttl_config(settings.checkpointer_ttl_s),
    )


def BI_CATALOG_NAMES() -> list[str]:
    """Report names, imported lazily to keep module import light."""
    from agentsys.connectors.badie_reports import CATALOG

    return sorted(CATALOG)


async def _bi_role_is_read_only(engine: Any) -> bool | None:  # noqa: ANN401
    """Ask the database whether the BI role really is read-only.

    Returns True / False, or None when the question could not be answered —
    those are three different situations and collapsing the third into either
    of the other two is the bug. "Could not determine" must not read as
    "determined to be writable" (that would take the app down whenever the
    reporting replica is briefly unreachable), and it must not read as
    "determined to be read-only" either (that would restore the very
    assumption this check exists to remove).

    The dedicated read-only role is the layer that is meant to hold even if
    parameter validation and the Layer-2 interceptor both have bugs, and
    nothing anywhere confirmed it was configured. `BI_DATABASE_URL` was
    trusted to point at a role someone had set up by hand.
    """
    from sqlalchemy.exc import SQLAlchemyError

    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SHOW default_transaction_read_only"))
            return str(result.scalar()).strip().lower() == "on"
    except SQLAlchemyError:
        structlog.get_logger().warning("bi.read_only_check_failed", exc_info=True)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manage startup / shutdown resources.

    D-014 S4 (carry-forward from the S1/S2/S3 gate): every teardown callback
    is registered on a single ``AsyncExitStack`` right after its resource is
    created, so a failure in one teardown (e.g. ``engine.dispose()`` raising)
    can never prevent the others (``whatsapp_client.aclose()``, the
    checkpointer context, ``close_redis_pool()``) from running —
    ``AsyncExitStack`` still invokes every registered callback even when an
    earlier one raises.
    """
    settings = get_settings()

    async with AsyncExitStack() as resource_stack:
        # Startup — create async engine and store on app state
        app.state.engine = get_engine(settings.database_url)
        resource_stack.push_async_callback(app.state.engine.dispose)

        # D-014 — outbound WhatsApp client (design AD-2), built once and shared.
        app.state.whatsapp_client = WhatsAppClient(
            httpx.AsyncClient(),
            phone_number_id=settings.whatsapp_phone_number_id,
            token=settings.whatsapp_token,
            base_url=settings.whatsapp_graph_api_url,
        )
        resource_stack.push_async_callback(app.state.whatsapp_client.aclose)

        resource_stack.push_async_callback(close_redis_pool)

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
            from agentsys.harness.loader import resolve
            from agentsys.services.embeddings import get_embedding_provider

            embedder = get_embedding_provider(settings)

            # D-023 — resolve the BI engine BEFORE the registry, so the
            # registry has a single registration point for `run_report`.
            # The tool is registered either way: platform/roles/data-agent
            # names it, and a tool a manifest names but the registry lacks
            # makes the whole role unbuildable through InjectionError — not
            # partially usable. Unbound, it answers that reporting is
            # unavailable. Sharing one registry is safe because the role
            # manifest is the gate: `sales-agent` does not list run_report,
            # so it never sees it. (Per-deployment registries are D-019.)
            bi_engine = None
            if settings.bi_database_url:
                # A DEDICATED engine on the read-only role (AD-3). Never
                # app.state.engine — that one can write, and the whole point
                # is that this path cannot, whatever the model asks for.
                #
                # Built through get_engine, like every other engine here, so
                # it gets pool_pre_ping. This is the engine most likely to sit
                # behind a connection its pool has held idle — a separate,
                # possibly remote, read-only replica — which makes it the
                # worst one to leave without stale-connection detection.
                candidate = get_engine(settings.bi_database_url)
                resource_stack.push_async_callback(candidate.dispose)

                read_only = await _bi_role_is_read_only(candidate)
                if read_only is False:
                    # Fail CLOSED. The read-only role is the guardrail that is
                    # supposed to hold even if validation and the interceptor
                    # both have bugs; nothing verified it, the URL was simply
                    # trusted to point somewhere someone configured by hand.
                    # If it can write, the guardrail is absent, so no engine
                    # gets bound and every call reports reporting unavailable.
                    _logger.error(
                        "bi.role_not_read_only",
                        message=(
                            "BI_DATABASE_URL points at a role with "
                            "default_transaction_read_only = off. run_report "
                            "is registered but UNBOUND and will refuse every "
                            "call. Fix the role, do not work around this."
                        ),
                    )
                else:
                    if read_only is None:
                        # "Could not determine" is not "determined to be
                        # writable". Coupling startup to the reporting replica
                        # being reachable would take the sales bot down for a
                        # BI dependency; the tool degrades at call time into a
                        # structured error instead.
                        _logger.warning(
                            "bi.read_only_unverified",
                            message=(
                                "Could not verify that BI_DATABASE_URL is "
                                "read-only — the database did not answer. "
                                "Binding run_report anyway."
                            ),
                        )
                    bi_engine = candidate
                    _logger.info("bi.tool_bound", reports=sorted(BI_CATALOG_NAMES()))
            else:
                _logger.warning(
                    "bi.disabled",
                    message=(
                        "BI_DATABASE_URL is not set — run_report is registered "
                        "but UNBOUND and will refuse every call."
                    ),
                )

            registry = build_badie_rag_registry(settings, embedder, bi_engine)

            session_provider = async_sessionmaker(
                app.state.engine, expire_on_commit=False
            )

            # Build chat model from configured provider
            model = _build_chat_model(settings.adapter_provider)

            # D-014 S4 (design AD-1/AD-7) — shared checkpointer, only built
            # when checkpointing is enabled at all. Injected into every
            # AgentRuntime; each run_turn call still opts in per-invocation
            # via thread_id (design AD-1).
            checkpointer = None
            if settings.whatsapp_checkpointer_enabled:
                checkpointer = await resource_stack.enter_async_context(
                    _build_checkpointer_cm(settings)
                )

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
                # D-014 AD-5 — data-driven grants: resolve the definition FIRST so
                # the role's own resolved permissions become granted_permissions.
                # No hardcoded role -> permissions map (discovery #184).
                definition = resolve(role, client=deployment)
                equipped = build_runtime(
                    role_type=role,
                    registry=registry,
                    granted_permissions=definition.permissions,
                    client=deployment,
                    session_provider=session_provider,
                )
                runtimes[model_id] = AgentRuntime(
                    runtime=equipped, model=model, checkpointer=checkpointer
                )
                _logger.info("adapter.runtime_cached", model_id=model_id)

            app.state.runtimes = runtimes
        else:
            app.state.runtimes = {}

        yield


# Sent when no API key is configured. ChatOpenAI rejects an empty key at
# construction time, but keyless OpenAI-compatible endpoints are legitimate,
# so the provider needs *something* to hand the client.
_NO_API_KEY_PLACEHOLDER = "not-required"


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

    if provider == "openai_compatible":
        from agentsys.agent.reasoning import ReasoningSanitizedChatOpenAI

        # Unlike the branches above, this one reads Settings instead of raw
        # os.getenv. The divergence is deliberate — unifying all four providers
        # on Settings belongs to D-016, not here. Please don't "fix" it.
        settings = get_settings()
        if not settings.openai_compatible_base_url:
            raise ValueError(
                "OPENAI_COMPATIBLE_BASE_URL is required when "
                "ADAPTER_PROVIDER=openai_compatible"
            )
        if not settings.openai_compatible_model:
            raise ValueError(
                "OPENAI_COMPATIBLE_MODEL is required when "
                "ADAPTER_PROVIDER=openai_compatible"
            )

        compatible_api_key = settings.openai_compatible_api_key
        if not compatible_api_key:
            # ChatOpenAI refuses to construct with None or "" even though
            # keyless OpenAI-compatible hosts (vLLM, LM Studio, llama.cpp) are
            # perfectly normal. Warn so a genuinely forgotten key shows up at
            # startup rather than as an unexplained 401 much later.
            structlog.get_logger().warning(
                "openai_compatible.no_api_key",
                base_url=settings.openai_compatible_base_url,
            )
            compatible_api_key = _NO_API_KEY_PLACEHOLDER

        return ReasoningSanitizedChatOpenAI(
            model=settings.openai_compatible_model,
            base_url=settings.openai_compatible_base_url,
            api_key=SecretStr(compatible_api_key),
            temperature=0,
        )

    # Default: ollama
    from langchain_ollama import ChatOllama

    return ChatOllama(model="qwen2.5:3b", temperature=0)


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
