# pyright: reportMissingImports=false, reportCallIssue=false
"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any, Literal

import httpx
import structlog
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from agents_system import __version__
from agents_system.agent.spec import Agent
from agents_system.config import Settings, get_settings
from agents_system.harness.loader import (
    _SAFE_SEGMENT,
    DefinitionError,
    RoleLocator,
    RootConfig,
)
from agents_system.harness.registry import RegistryFactory
from agents_system.integration import openai_router, webhook_router
from agents_system.integration.whatsapp_client import WhatsAppClient
from agents_system.models.base import get_engine, get_session_factory
from agents_system.observability import RequestIdMiddleware, setup_logging
from agents_system.observability.metrics import DEFAULT_REGISTRY
from agents_system.services.admission import TurnAdmissionLimiter
from agents_system.services.db_role import role_is_read_only
from agents_system.services.outbox import (
    DEFAULT_LEASE_DURATION,
    OutboxBacklogCounts,
    count_outbox_backlog,
)
from agents_system.services.participants import (
    ConversationRecorder,
    ParticipantDirectory,
)
from agents_system.services.redis import close_redis_pool, get_redis_client


def _checkpointer_ttl_config(checkpointer_ttl_s: int | None) -> dict[str, Any] | None:
    """Convert AD-7's ``checkpointer_ttl_s`` (seconds) into the
    langgraph-checkpoint-redis package's ``ttl`` dict shape, whose
    ``default_ttl`` is expressed in MINUTES."""
    if checkpointer_ttl_s is None:
        return None
    return {"default_ttl": checkpointer_ttl_s / 60, "refresh_on_read": True}


def _build_checkpointer_cm(settings: Settings) -> Any:
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


async def _bi_role_is_read_only(engine: Any) -> bool | None:
    """Ask the database whether the BI role really is read-only.

    Thin BI-scoped wrapper over the shared
    :func:`agents_system.services.db_role.role_is_read_only` (the full
    True/False/None reasoning lives there — see its docstring). Kept as its
    own function, rather than calling the shared one directly at the
    lifespan call site below, so `bi.read_only_check_failed` stays this
    deployment's own log event name and so any future BI-specific behaviour
    has a place to live without touching the shared helper.
    """
    return await role_is_read_only(engine, log_event="bi.read_only_check_failed")


#: #78 Phase 0 Slice 2 -- same HTTPBearer mechanism as
#: integration/openai_adapter.py's `_http_bearer` (a distinct instance: the
#: two endpoints are protected by two independent keys/settings fields).
_metrics_http_bearer = HTTPBearer(auto_error=False)


async def verify_metrics_access(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_metrics_http_bearer)
    ] = None,
) -> None:
    """FastAPI dependency gating `GET /metrics` (#78 Phase 0 Slice 2).

    Follows `integration/openai_adapter.py`'s `verify_bearer` fail-closed
    spirit, with one extra step in front of it (ADR-005 section 6 / issue
    #78's scope note: "unauthenticated exposure only under an explicit
    setting"):

    - `metrics_enabled` is `False` (the default): 404 -- the endpoint does
      not exist. Absent, not merely unauthenticated, so probing for it
      reveals nothing about whether metrics are configured at all.
    - `metrics_enabled` is `True` + `metrics_api_key` set: Bearer auth
      enforced (constant-time compare), identical shape to `verify_bearer`.
    - `metrics_enabled` is `True` + `metrics_api_key` empty: open. Refused
      at boot by `Settings.validate_security_fail_closed` unless
      `allow_insecure=True` was also set explicitly (dev-only escape hatch,
      same as the adapter's own open mode).
    """
    settings = get_settings()
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404)

    expected_key = settings.metrics_api_key
    if not expected_key:
        # Open mode — Settings itself already refused to boot this way
        # without allow_insecure=True.
        return

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header (expected Bearer token).",
        )

    if not secrets.compare_digest(credentials.credentials, expected_key):
        raise HTTPException(status_code=401, detail="Invalid bearer token.")


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
    # ADR-004 D5 -- the runtimes to build, from `create_app(agents=...)` or,
    # when it got none, from Settings. Resolved here, before any resource
    # exists, so an invalid registration fails boot with nothing to unwind.
    plan = _boot_plan(app, settings)

    # #78 Phase 0 Slice 2 -- unconditional (unlike the ADAPTER_API_KEY
    # warning below, which is scoped to `required_runtimes`): /metrics has
    # no channel/runtime dependency at all, so this is the only place that
    # sees every boot where it would be open.
    if settings.metrics_enabled and not settings.metrics_api_key:
        # Reachable only under ALLOW_INSECURE=true -- the Settings validator
        # fails closed otherwise (see validate_security_fail_closed).
        structlog.get_logger().warning(
            "metrics.open_mode",
            message=(
                "METRICS_API_KEY is not set and ALLOW_INSECURE=true. "
                "GET /metrics is OPEN — never do this in production."
            ),
        )

    async with AsyncExitStack() as resource_stack:
        # Startup — create async engine and store on app state
        app.state.engine = get_engine(settings.database_url)
        resource_stack.push_async_callback(app.state.engine.dispose)

        # #46 (ADR-001 D-033) — one process-wide bound on concurrent turns,
        # shared by every entry point that runs one: the webhook worker
        # below AND POST /v1/chat/completions (integration/openai_adapter.py
        # reads it off app.state). No teardown needed — it holds no resource.
        app.state.turn_admission_limiter = TurnAdmissionLimiter(
            settings.max_concurrent_turns
        )

        # D-007 — AuditSink: async fire-and-forget event sink.
        from agents_system.audit.sink import AuditSink

        # get_session_factory takes the engine, not the URL (imported at
        # module level above — also used by GET /health's backlog query).
        # app.state.engine is already built and disposed by resource_stack
        # above.
        _audit_session_factory = get_session_factory(app.state.engine)
        audit_sink = AuditSink(session_factory=_audit_session_factory)
        await audit_sink.start()
        AuditSink.set_current(audit_sink)
        app.state.audit_sink = audit_sink
        resource_stack.push_async_callback(audit_sink.stop)

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
        _logger = structlog.get_logger()
        if settings.adapter_runtimes and not settings.adapter_api_key:
            # Reachable only under ALLOW_INSECURE=true — the Settings
            # validator fails closed otherwise (D-014 S5, BLOCKER 1).
            # Guarded on `adapter_runtimes`, not on every registration: a
            # runtime built for another channel is never published on /v1,
            # so it is not what this warning is about.
            _logger.warning(
                "adapter.open_mode",
                message=(
                    "ADAPTER_API_KEY is not set and ALLOW_INSECURE=true. "
                    "The /v1/* endpoints are OPEN — never do this in production."
                ),
            )

        if plan.registrations:
            from agents_system.agent.graph import AgentRuntime, _effective_limits
            from agents_system.harness.factory import build_runtime
            from agents_system.harness.loader import resolve
            from agents_system.services.embeddings import get_embedding_provider

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
                if read_only is not None and not read_only:
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
                    _logger.info("bi.tool_bound", read_only_verified=True)
            else:
                _logger.warning(
                    "bi.disabled",
                    message=(
                        "BI_DATABASE_URL is not set — run_report is registered "
                        "but UNBOUND and will refuse every call."
                    ),
                )

            # The tool registry is the caller's: it is one deployment's
            # wiring of connectors to its own data, and the platform has
            # none. `create_app` stashed the factory on app.state.
            registry = app.state.registry_factory(settings, embedder, bi_engine)

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

            roots: RootConfig | None = getattr(app.state, "roots", None)

            runtimes: dict[str, AgentRuntime] = {}
            for runtime_id, registration in plan.registrations.items():
                client = registration.client
                if client is not None and roots is None:
                    raise DefinitionError(
                        f"Runtime {runtime_id!r} specifies client override {client!r}, "
                        "which requires an explicit RootConfig(deployments_root=...) passed to create_app(). "
                        "agents_system does not derive a default deployments_root for client overrides."
                    )
                # `roots` is the exact caller-supplied RootConfig object passed to
                # create_app (or None for generic roles). Resolving here only
                # gets the role/untrusted_input/limits facts this loop needs
                # below -- the permission GRANT itself is looked up
                # separately (`plan.grants`), immediately before
                # build_runtime: permission-model PR3 (issue #38) removed
                # AD-5's auto-grant-of-the-role's-full-permission-set, so
                # `definition.permissions` (what the role DECLARES it may
                # need) is no longer treated as what a deployment GRANTS it.
                definition = resolve(
                    registration.locator,
                    client=client,
                    roots=roots,
                )

                # ADR-002 C.13 — a channel must only ever be bound to a role
                # marked safe for the input it delivers. WhatsApp is
                # attacker-reachable external input; check the resolved
                # definition BEFORE build_runtime does any work and BEFORE
                # app.state.runtimes is populated, so a misconfigured
                # deployment (e.g. an untrusted_input=false role bound to
                # WhatsApp) is a boot failure, not a per-message surprise
                # caught (or missed) later. Scoped to the exact WhatsApp
                # runtime id only — the OpenAI adapter is an accepted risk,
                # not enforced here (see ADR-002 C.13 "OpenAI adapter").
                if (
                    settings.whatsapp_runtime_id
                    and runtime_id == settings.whatsapp_runtime_id
                    and not definition.untrusted_input
                ):
                    raise DefinitionError(
                        f"Channel 'whatsapp' is bound to {registration.subject} "
                        f"(runtime {runtime_id!r}), which resolves "
                        "untrusted_input=False. WhatsApp delivers "
                        "untrusted external input, so it must "
                        "declare untrusted_input: true (ADR-002 C.11/C.13) "
                        "to be bound to it. Refusing to boot."
                    )

                # A WhatsApp turn must finish before its deferred-webhook lease
                # expires. Reserve explicit non-turn headroom for surrounding
                # persistence and provider-send work; adapter-only runtimes do
                # not run under this lease and remain unconstrained by it.
                if runtime_id == settings.whatsapp_runtime_id:
                    effective_limits = _effective_limits(definition.execution_limits)
                    total_timeout_s = effective_limits["total_execution_timeout_s"]
                    non_turn_headroom_s = 60
                    lease_seconds = DEFAULT_LEASE_DURATION.total_seconds()
                    if total_timeout_s + non_turn_headroom_s >= lease_seconds:
                        raise ValueError(
                            f"WhatsApp runtime {runtime_id!r} has "
                            f"total_execution_timeout_s={total_timeout_s}; with "
                            f"non_turn_headroom_s={non_turn_headroom_s}, it must be "
                            f"less than outbox lease_seconds={lease_seconds}"
                        )

                # permission-model PR3 (issue #38, design.md Resolved
                # Decision 5) -- the grant source is the SOLE grant for this
                # boot path. A configured runtime with no matching entry
                # fails boot loudly (same style as the whatsapp_runtime_id
                # failure below) rather than silently defaulting to an empty
                # or full-role grant.
                if runtime_id not in plan.grants:
                    example = (
                        f'DEPLOY_GRANTS=\'{{"{runtime_id}": ["read:catalog"]}}\''
                        if plan.grant_source == "DEPLOY_GRANTS"
                        else f"grants={{{runtime_id!r}: ['read:catalog']}}"
                    )
                    raise DefinitionError(
                        f"Runtime {runtime_id!r} ({registration.subject}) has no "
                        f"{plan.grant_source} entry. Boot requires an explicit "
                        "deploy-time grant for every configured runtime -- "
                        f"map this runtime id in {plan.grant_source} to its "
                        f"granted permission wire names, e.g. {example}. "
                        "Refusing to boot."
                    )

                # The SAME explicit root as the `resolve` above. Passing it
                # to only one of the two was the whole bug in a subtler form:
                # the definition used for the permission grant came from the
                # explicit path while the runtime actually installed -- its
                # tool surface and its skill files -- resolved against the
                # library's guessed default.
                equipped = build_runtime(
                    role_type=registration.locator,
                    registry=registry,
                    granted_permissions=plan.grants[runtime_id],
                    client=client,
                    roots=roots,
                    session_provider=session_provider,
                )
                # #78 Phase 0 Slice 2 -- runtime_id=runtime_id labels this
                # runtime's metrics with its registered runtime id (the
                # opaque key from create_app(agents=...) or
                # AGENT_REGISTRATIONS) instead of AgentRuntime's own derived
                # provider-model-id default (see AgentRuntime's `runtime_id`
                # parameter docstring in agent/graph.py).
                runtimes[runtime_id] = AgentRuntime(
                    runtime=equipped,
                    model=model,
                    checkpointer=checkpointer,
                    runtime_id=runtime_id,
                )
                _logger.info("adapter.runtime_cached", model_id=runtime_id)

            app.state.runtimes = runtimes
            # What /v1 may publish, which is NOT the whole cache. The cache
            # covers every channel; this is only what the operator named.
            app.state.adapter_model_ids = frozenset(settings.adapter_runtimes)
        else:
            app.state.runtimes = {}
            app.state.adapter_model_ids = frozenset()

        # W2b2 — the deferred webhook worker. Started LAST, after every
        # dependency it needs (engine, whatsapp_client, runtimes, the
        # participant directory / recorder create_app stashed on app.state)
        # already exists, and its stop() is pushed last so AsyncExitStack's
        # LIFO teardown runs it FIRST — before any of those dependencies are
        # torn down. Its own cancellation-safety (a claim left mid-flight
        # keeps its lease, recoverable once that lease expires) is
        # ``DeferredWebhookWorker``'s, reusing W2a's lease primitives; this
        # lifespan only owns when start()/stop() run.
        webhook_runtime = (
            app.state.runtimes.get(settings.whatsapp_runtime_id)
            if settings.whatsapp_runtime_id
            else None
        )
        if webhook_runtime is not None:
            from agents_system.services.webhook_worker import DeferredWebhookWorker

            webhook_worker = DeferredWebhookWorker(
                session_factory=get_session_factory(app.state.engine),
                worker_id=f"webhook-worker-{uuid.uuid4()}",
                directory=app.state.participant_directory,
                runtime=webhook_runtime,
                whatsapp_client=app.state.whatsapp_client,
                recorder=app.state.conversation_recorder,
                checkpointer_enabled=settings.whatsapp_checkpointer_enabled,
                poll_interval_s=settings.webhook_worker_poll_interval_s,
                claim_limit=settings.webhook_worker_claim_limit,
                admission_limiter=app.state.turn_admission_limiter,
            )
            await webhook_worker.start()
            resource_stack.push_async_callback(webhook_worker.stop)
            app.state.webhook_worker = webhook_worker
        elif settings.whatsapp_token and settings.whatsapp_phone_number_id:
            # #141 review follow-up -- WhatsApp is otherwise fully
            # configured to receive AND reply (both outbound credentials
            # set), yet `whatsapp_runtime_id` is empty. `POST /webhook` is
            # mounted
            # UNCONDITIONALLY (`include_router(webhook_router)` below) and
            # durably accepts every signed inbound message regardless of
            # this value, so the old behaviour -- warn and boot anyway --
            # let a fully signed, fully credentialed deployment accept
            # messages that no worker would EVER process, with only a
            # startup warning line as the signal. Fail closed instead: this
            # is a boot-time misconfiguration, not a runtime condition to
            # expose via backlog metrics.
            #
            # A truthy `whatsapp_runtime_id` cannot reach this branch while
            # itself being the reason `webhook_runtime` is `None`:
            # `_boot_plan` refuses it unless a registration (from
            # `create_app(agents=...)` or AGENT_REGISTRATIONS) matches it;
            # every registration reaches the runtime loop above, which
            # either raises before this code runs
            # (an unsafe `untrusted_input` role, an execution timeout too
            # close to the outbox lease, a missing grant) or lands in
            # `runtimes[runtime_id]` -- there is no silent "resolved but not
            # cached" path for a registered id. If that stops being true,
            # a registered-but-unresolved id must first be covered by a
            # test before this message claims it again.
            raise DefinitionError(
                "WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID are configured "
                f"but whatsapp_runtime_id={settings.whatsapp_runtime_id!r} "
                "resolves to no runtime (unset). "
                "POST /webhook would durably accept signed WhatsApp messages "
                "that the deferred worker would never process. Set "
                "WHATSAPP_RUNTIME_ID to a valid runtime id, or unset "
                "WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID if this deployment "
                "does not use WhatsApp. Refusing to boot."
            )
        else:
            structlog.get_logger().warning(
                "webhook_worker.no_runtime_resolved",
                whatsapp_runtime_id=settings.whatsapp_runtime_id,
                detail=(
                    "no runtime resolved for whatsapp_runtime_id; the "
                    "deferred webhook worker did not start. Durable inbound "
                    "work still accumulates and waits -- it is not dropped."
                ),
            )
            app.state.webhook_worker = None

        yield


# Sent when no API key is configured. ChatOpenAI rejects an empty key at
# construction time, but keyless OpenAI-compatible endpoints are legitimate,
# so the provider needs *something* to hand the client.
_NO_API_KEY_PLACEHOLDER = "not-required"


def _build_chat_model(provider: str) -> Any:
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
        from agents_system.agent.reasoning import ReasoningSanitizedChatOpenAI

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

    # Default: ollama. Model + base URL come from Settings (#169, ADR-002
    # E.18) instead of being hardcoded, so a role can be pointed at a
    # different local model (e.g. the live-eval pipeline) without a code
    # change. An empty ollama_base_url becomes None so ChatOllama falls back
    # to its own default host resolution rather than treating "" as a URL.
    from langchain_ollama import ChatOllama

    ollama_settings = get_settings()
    return ChatOllama(
        model=ollama_settings.ollama_model,
        base_url=ollama_settings.ollama_base_url or None,
        temperature=0,
    )


async def _outbox_backlog(engine: Any) -> OutboxBacklogCounts | None:
    """Best-effort outbox backlog counts for GET /health (#141).

    ``None`` on any failure -- opening a session or querying can fail for
    the same reasons the postgres probe can (already reported separately as
    ``postgres_status``), and that must not crash the health check itself.
    ``None`` is deliberately not coerced to ``0``: an unreachable/unknown
    backlog is a different fact than a confirmed-empty one, and collapsing
    them would let a DB outage report a falsely healthy backlog.

    Bounded to the same 3s budget as the postgres/redis probes above (#141
    review follow-up, BLOCKER) -- ``outbox_work`` is queried through the
    same engine those probes use, and without a timeout here a locked or
    slow table would hang this whole endpoint (and any liveness probe
    reading it) even while postgres/redis themselves answer fine. A timeout
    is just another failure from this function's point of view: it is
    caught below and reported as ``None``, the same as any other error.
    """
    try:
        async with asyncio.timeout(3.0):
            session_factory = get_session_factory(engine)
            async with session_factory() as session:
                return await count_outbox_backlog(session)
    except Exception:
        structlog.get_logger().warning("health.outbox_backlog_error")
        return None


_MAX_RUNTIME_ID_LENGTH = 64
"""A runtime id lands in URLs, every log line of its turns and /metrics
labels, so it is short by contract, not only safe by character class."""


def _validate_runtime_id(runtime_id: str) -> str:
    """Reject a runtime id that is empty, longer than
    ``_MAX_RUNTIME_ID_LENGTH``, or contains anything other than letters,
    digits, underscore or hyphen (design.md D6).

    A runtime id is deployer-chosen (design.md D5) and becomes part of
    URLs (``/v1/models``), structured logs and metrics labels once
    registered -- the same class of user-supplied, filesystem-adjacent
    segment ``loader._SAFE_SEGMENT`` already validates elsewhere in this
    codebase (a role type, a client/deployment name), reused here for the
    same reason: consistency, and because an id built from it must be safe
    to embed in a URL path segment and a log line with no further escaping.
    Malformed ids fail here, at registration time, rather than surfacing as
    a confusing lookup miss at first request.
    """
    if not isinstance(runtime_id, str):
        raise DefinitionError(
            f"Invalid runtime id of type {type(runtime_id).__name__}: a "
            "runtime id must be a str."
        )
    if len(runtime_id) > _MAX_RUNTIME_ID_LENGTH:
        # Never echo the whole value: the length is the problem.
        raise DefinitionError(
            f"Invalid runtime id {runtime_id[:_MAX_RUNTIME_ID_LENGTH]!r}... "
            f"({len(runtime_id)} characters). A runtime id is at most "
            f"{_MAX_RUNTIME_ID_LENGTH} characters."
        )
    if not _SAFE_SEGMENT.fullmatch(runtime_id):
        raise DefinitionError(
            f"Invalid runtime id {runtime_id!r}. Must match "
            f"{_SAFE_SEGMENT.pattern} -- letters, digits, underscore and "
            "hyphen only, non-empty, not starting with '-' or '_'."
        )
    return runtime_id


def _validate_client(runtime_id: str, client: object) -> str:
    """Reject a ``clients`` value that is not a client name: a ``str``
    matching ``loader._SAFE_SEGMENT``, the rule the loader applies when it
    joins that name onto ``deployments_root``.

    A ``None`` (``os.environ.get(...)`` for an unset variable) would
    otherwise pass as "no client" and serve the role WITHOUT its
    subtractive override, silently. The error names the id, never the
    value.
    """
    if not isinstance(client, str):
        raise DefinitionError(
            f"clients[{runtime_id!r}] must be a deployment client name (str), "
            f"got {type(client).__name__}. Refusing to boot."
        )
    if not _SAFE_SEGMENT.fullmatch(client):
        raise DefinitionError(
            f"clients[{runtime_id!r}] is not a valid deployment client name. "
            f"It must match {_SAFE_SEGMENT.pattern} -- letters, digits, "
            "underscore and hyphen only, non-empty, not starting with '-' or "
            "'_'. Refusing to boot."
        )
    return client


def _parse_agent_registration(value: str) -> tuple[str, str | None]:
    """Read one ``AGENT_REGISTRATIONS`` value: ``"{role}"`` or
    ``"{role}@{client}"`` -> ``(role, client or None)`` (design.md D5/D6).

    ``main.py``'s own env-boot convention, not a public string format: a
    library caller registers ``Agent``/``str`` objects through
    ``create_app(agents=...)`` instead. Strict -- at most one ``@``, and
    both the role and the client must match ``loader._SAFE_SEGMENT``;
    anything else fails boot naming the value, cut to
    ``_MAX_RUNTIME_ID_LENGTH`` characters. Nothing else in the value has a
    meaning: ``__`` in particular is part of the role name, never a
    separator.
    """
    if not isinstance(value, str):
        raise DefinitionError(
            f"Invalid agent registration of type {type(value).__name__}: "
            'expected "{role}" or "{role}@{client}".'
        )
    role, separator, client = value.partition("@")
    if not _SAFE_SEGMENT.fullmatch(role) or (
        separator and not _SAFE_SEGMENT.fullmatch(client)
    ):
        shown = (
            f"{value!r}"
            if len(value) <= _MAX_RUNTIME_ID_LENGTH
            else f"{value[:_MAX_RUNTIME_ID_LENGTH]!r}... ({len(value)} characters)"
        )
        raise DefinitionError(
            f"Invalid agent registration {shown}. Expected "
            '"{role}" or "{role}@{client}", where the role and the client '
            f"each match {_SAFE_SEGMENT.pattern} and '@' appears at most once."
        )
    return role, (client if separator else None)


@dataclasses.dataclass(frozen=True)
class _Registration:
    """One runtime the lifespan builds (design.md D5): what to resolve, and
    the deployment client (if any) to resolve it for."""

    locator: RoleLocator
    client: str | None
    subject: str
    """``role 'sales-agent'`` or ``agent 'triage-bot'`` -- names it in boot errors."""


@dataclasses.dataclass(frozen=True)
class _BootPlan:
    """Every runtime to build, keyed by runtime id, and where its grants come from."""

    registrations: dict[str, _Registration]
    grants: Mapping[str, Any]
    grant_source: str
    """``create_app(grants=...)``, or ``DEPLOY_GRANTS`` when it got none."""


def _build_registrations(
    agents: Mapping[str, Agent | str],
    clients: Mapping[str, str] | None,
) -> dict[str, _Registration]:
    """``create_app(agents=...)``, or ``AGENT_REGISTRATIONS`` read into the
    same shape: each id is opaque and validated, never parsed. A ``str`` is
    a predefined-role name; an ``Agent`` is its own locator."""
    clients = clients or {}
    # A client names a subtractive deployment override: one keyed by a
    # typo'd id would silently serve that agent WITHOUT its narrowing.
    unknown = [repr(key) for key in clients if key not in agents]
    if unknown:
        raise DefinitionError(
            f"clients names runtime id(s) {', '.join(unknown)} that agents "
            "does not register. Refusing to boot."
        )
    registrations: dict[str, _Registration] = {}
    for runtime_id, agent in agents.items():
        _validate_runtime_id(runtime_id)
        if isinstance(agent, Agent):
            if runtime_id in clients:
                # design.md D5 / Q5: deployment overrides stay
                # predefined-role-only. Fail loud, not a silent no-op.
                raise DefinitionError(
                    f"clients[{runtime_id!r}] is set, but agents[{runtime_id!r}] "
                    f"is an Agent ({agent.name!r}). A client deployment "
                    "override applies only to a predefined role registered "
                    "by name (a str). Refusing to boot."
                )
            registrations[runtime_id] = _Registration(
                agent._to_locator(), None, f"agent {agent.name!r}"
            )
        elif isinstance(agent, str):
            client = (
                _validate_client(runtime_id, clients[runtime_id])
                if runtime_id in clients
                else None
            )
            registrations[runtime_id] = _Registration(agent, client, f"role {agent!r}")
        else:
            raise DefinitionError(
                f"agents[{runtime_id!r}] must be an Agent or a predefined role "
                f"name (str), got {type(agent).__name__}."
            )
    return registrations


def _settings_agents(
    agent_registrations: Mapping[str, str],
) -> tuple[dict[str, Agent | str], dict[str, str]]:
    """The Settings-driven fallback, used when ``create_app`` got no
    ``agents``: ``AGENT_REGISTRATIONS`` read into the ``agents``/``clients``
    shape ``create_app`` takes, so both paths build through one loop
    (design.md D5). A malformed id or value fails boot, naming it."""
    agents: dict[str, Agent | str] = {}
    clients: dict[str, str] = {}
    for runtime_id, value in agent_registrations.items():
        try:
            _validate_runtime_id(runtime_id)
        except DefinitionError as exc:
            raise DefinitionError(
                f"AGENT_REGISTRATIONS: {exc} Refusing to boot."
            ) from exc
        try:
            role, client = _parse_agent_registration(value)
        except DefinitionError as exc:
            raise DefinitionError(
                f"AGENT_REGISTRATIONS[{runtime_id!r}]: {exc} Refusing to boot."
            ) from exc
        agents[runtime_id] = role
        if client is not None:
            clients[runtime_id] = client
    return agents, clients


def _boot_plan(app: FastAPI, settings: Settings) -> _BootPlan:
    """Which runtimes to build, validated before the lifespan creates any
    resource, so a bad registration fails boot before anything is served."""
    agents: Mapping[str, Agent | str] | None = getattr(app.state, "agents", None)
    grants: Mapping[str, Any] | None = getattr(app.state, "grants", None)
    clients: Mapping[str, str] | None = getattr(app.state, "clients", None)

    for runtime_id, granted in (grants or {}).items():
        # A str (or bytes) satisfies Sequence[str] but would be granted one
        # character at a time; a non-iterable cannot be a grant at all.
        if isinstance(granted, str | bytes) or not isinstance(granted, Iterable):
            raise DefinitionError(
                f"grants[{runtime_id!r}] must be a list of permission wire "
                f"names, got {type(granted).__name__}. A bare string is not "
                "a grant list. Refusing to boot."
            )

    if agents is None:
        if clients is not None:
            raise DefinitionError(
                "create_app got clients but no agents: clients names the "
                "deployment client of a registered predefined role, so it "
                "needs agents to register one. Refusing to boot."
            )
        agents, clients = _settings_agents(settings.agent_registrations)
        source = "AGENT_REGISTRATIONS"
        # Static, never derived from the id: nothing parses it.
        hint = (
            " A runtime id is no longer read as '{deployment}__{role}': map "
            'it in AGENT_REGISTRATIONS to "{role}" or "{role}@{client}".'
        )
    else:
        source = "create_app(agents=...)"
        hint = ""
    registrations = _build_registrations(agents, clients)

    # A channel id is looked up in the registration as an opaque key, never
    # parsed. One that matches nothing fails boot: an unmatched
    # WHATSAPP_RUNTIME_ID would accept messages no runtime answers, and an
    # unmatched ADAPTER_RUNTIMES id would leave /v1/models listing less than
    # the operator named.
    whatsapp_ids = (
        [settings.whatsapp_runtime_id] if settings.whatsapp_runtime_id else []
    )
    for channel, ids in (
        ("WHATSAPP_RUNTIME_ID", whatsapp_ids),
        ("ADAPTER_RUNTIMES", settings.adapter_runtimes),
    ):
        unmatched = [
            repr(runtime_id) for runtime_id in ids if runtime_id not in registrations
        ]
        if unmatched:
            raise DefinitionError(
                f"{channel} names runtime id(s) {', '.join(unmatched)} that "
                f"{source} does not register (registered: "
                f"{sorted(registrations)}).{hint} Refusing to boot."
            )

    return _BootPlan(
        registrations=registrations,
        grants=grants if grants is not None else settings.deploy_grants,
        grant_source="create_app(grants=...)"
        if grants is not None
        else "DEPLOY_GRANTS",
    )


def create_app(
    *,
    registry_factory: RegistryFactory,
    agents: Mapping[str, Agent | str] | None = None,
    grants: Mapping[str, Sequence[str]] | None = None,
    clients: Mapping[str, str] | None = None,
    participant_directory: ParticipantDirectory | None = None,
    conversation_recorder: ConversationRecorder | None = None,
    roots: RootConfig | None = None,
    title: str = "agents_system",
) -> FastAPI:
    """Application factory. Returns a configured FastAPI instance.

    Everything this function knows how to build -- the middleware, the two
    routers, `/health`, and the lifespan's engine, audit sink, Redis pool,
    runtime cache and checkpointer -- is the same for every deployment. The
    arguments below are the parts that are not, and the platform has no
    default for any of them because it owns no connectors, no identity
    schema and no conversation history.

    They are stashed on `app.state` rather than closed over, because the
    lifespan receives the app and not this scope -- the same mechanism
    `app.state.engine` already uses.

    Parameters
    ----------
    registry_factory:
        Called by the lifespan as ``(settings, embedder, bi_engine)`` once
        those are resolved, and must return the ``ToolRegistry`` the role
        manifests are injected against.
    agents:
        Deployer-chosen runtime id -> either an ``Agent`` (a custom,
        library-defined agent) or a bare ``str`` (a predefined platform-role
        name, resolved with no deployment override unless ``clients`` names
        one). Every
        id is opaque, never parsed: at most 64 letters, digits, ``_`` or
        ``-``, not starting with ``_`` or ``-``. The lifespan builds a
        runtime for EVERY entry at boot, and one entry that fails to resolve
        or equip fails the whole boot. ``WHATSAPP_RUNTIME_ID`` and each
        ``ADAPTER_RUNTIMES`` id must be a key here, or boot fails naming it;
        ``/v1/models`` lists only the ``ADAPTER_RUNTIMES`` ids. ``None`` (the
        default) registers the ``AGENT_REGISTRATIONS`` entries instead
        (``{id: "role" | "role@client"}``, each a predefined role), through
        the same rules; a caller that never passes ``agents`` (e.g.
        ``demo.py``'s ``build_app``) needs no change. When ``agents`` is
        given, ``AGENT_REGISTRATIONS`` is ignored, never merged.
    grants:
        Runtime id -> granted permission wire names (a list, never a bare
        string), keyed by the same ids as ``agents``. Nothing is granted
        automatically: a registered id with no entry fails boot. ``None``
        falls back to ``settings.deploy_grants`` (``DEPLOY_GRANTS``), keyed
        by the same ids.
    clients:
        Runtime id -> deployment client name, valid only for a registered
        ``str`` (predefined role) entry. An entry for an ``Agent``, for an
        id ``agents`` does not register, or without ``agents`` fails boot,
        and so does a value that is not a client-name ``str`` (``None``
        included: leave the id out for no client).
    participant_directory:
        Resolves an inbound channel address to an identity. Absent means the
        inbound route fails closed and runs no turn.
    conversation_recorder:
        Records completed turns. Absent means none are recorded.
    roots:
        Consumer-owned path configuration for platform and deployment roles.
        A client override (``clients[id]``, or an ``AGENT_REGISTRATIONS``
        value ``"{role}@{client}"``) requires an explicit RootConfig
        specifying deployments_root.
    title:
        OpenAPI title.
    """
    setup_logging()

    settings = get_settings()
    application = FastAPI(
        title=title,
        version=__version__,
        debug=settings.debug,
        lifespan=lifespan,
    )

    application.state.registry_factory = registry_factory
    application.state.agents = agents
    application.state.grants = grants
    application.state.clients = clients
    application.state.participant_directory = participant_directory
    application.state.conversation_recorder = conversation_recorder
    application.state.roots = roots

    application.add_middleware(RequestIdMiddleware)
    application.include_router(webhook_router)
    application.include_router(openai_router)

    logger = structlog.get_logger()

    @application.get("/health")
    async def health(
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
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

        # #141 -- webhook worker liveness + outbox backlog depth. Absent
        # `app.state.webhook_worker` (a lifespan that never ran, or a
        # deployment with no WhatsApp runtime at all) reads as "not running",
        # not an error -- a worker-less deployment with an empty backlog is
        # healthy.
        worker = getattr(request.app.state, "webhook_worker", None)
        worker_running = worker is not None and worker.is_running

        backlog = await _outbox_backlog(engine)
        outbox_pending = backlog.pending if backlog is not None else None
        outbox_leased = backlog.leased if backlog is not None else None
        outbox_leased_expired = backlog.leased_expired if backlog is not None else None

        # Acceptance criterion (#141): degraded when the worker is not
        # running WHILE work is pending -- an unknown backlog (`None`, e.g.
        # the DB is already down and reported via postgres_status) never
        # degrades on its own; a nonzero backlog with a live worker doesn't
        # either, since the worker is actively draining it.
        #
        # #141 review follow-up (BLOCKER): the same applies to abandoned
        # leases. `outbox_leased` alone hides them -- it also counts leases
        # a currently-running worker still legitimately holds -- so this
        # checks `outbox_leased_expired` specifically: rows whose lease is
        # already past its expiry, i.e. claimed by a worker that is no
        # longer running to reclaim or finish them. A live lease with no
        # running worker cannot happen in this single-worker-per-process
        # model without also being `outbox_leased_expired` once it expires,
        # so this does not need its own "worker not running" gate beyond
        # the one already shared with `backlog_pending_undrained`.
        backlog_pending_undrained = (
            outbox_pending is not None and outbox_pending > 0 and not worker_running
        )
        backlog_lease_abandoned = (
            outbox_leased_expired is not None
            and outbox_leased_expired > 0
            and not worker_running
        )
        backlog_undrained = backlog_pending_undrained or backlog_lease_abandoned

        overall: Literal["ok", "degraded"] = (
            "ok"
            if postgres_status == "ok"
            and redis_status == "ok"
            and not backlog_undrained
            else "degraded"
        )

        return {
            "status": overall,
            "environment": settings.environment,
            "postgres": postgres_status,
            "redis": redis_status,
            "webhook_worker": {
                "running": worker_running,
                "outbox_pending": outbox_pending,
                "outbox_leased": outbox_leased,
                "outbox_leased_expired": outbox_leased_expired,
            },
        }

    @application.get("/metrics", dependencies=[Depends(verify_metrics_access)])
    async def metrics() -> Response:
        """Prometheus text exposition of the process-wide metrics registry
        (#78 Phase 0 Slice 2). Access is gated by `verify_metrics_access`
        above -- absent (404) unless `metrics_enabled` is set, then bearer-
        protected exactly like `integration/openai_adapter.py`'s
        `verify_bearer` whenever `metrics_api_key` is configured.
        """
        return Response(
            content=generate_latest(DEFAULT_REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )

    return application
