# pyright: reportMissingImports=false, reportCallIssue=false
"""FastAPI application factory.

Where the code lives: ``lifespan`` and ``create_app`` stay here; the helpers
they call live in ``agents_system.serving``, one module per concern
(``stores``, ``auth``, ``models``, ``health``, ``registrations``,
``channels`` and ``boot``). The public names keep this import path.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import (
    AsyncIterator,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import AsyncExitStack, asynccontextmanager
from typing import TYPE_CHECKING, Any, Literal

import httpx
import structlog
from langchain_core.language_models import BaseChatModel
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from agents_system.extras import MissingExtraError, raise_missing_extra
from agents_system.serving.auth import (
    verify_metrics_access,
)
from agents_system.serving.boot import (
    _boot_plan,
    _warn_if_turn_budget_below_request_timeout,
)
from agents_system.serving.channels import (
    _check_channel_runtime,
    _fixed_resolver,
    _registered_channels,
    _validate_channels,
)
from agents_system.serving.health import (
    ProviderHealthProbe,
    _outbox_backlog,
)
from agents_system.serving.models import (
    _build_chat_model,
)
from agents_system.serving.registrations import (
    _refuse_builtin_default_tools,
)
from agents_system.serving.stores import (
    _bi_role_is_read_only,
    _bind_proactive_store,
    _bind_schedule_store,
    _bind_user_fact_store,
    _build_checkpoint_purger,
    _build_checkpointer_cm,
    _build_usage_ledger,
    _start_scope_provider,
)

try:
    from fastapi import Depends, FastAPI, Request
    from fastapi.responses import JSONResponse, Response
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker
except ImportError as exc:
    raise_missing_extra("server", "The served app", exc)

from agents_system import __version__
from agents_system.agent import models as _models
from agents_system.agent.spec import Agent
from agents_system.channels.port import (
    Channel,
    ChannelResolver,
    InboundChannels,
)
from agents_system.channels.whatsapp import (
    WHATSAPP_CHANNEL,
    WhatsAppChannel,
    WhatsAppChannelSettings,
)
from agents_system.config import Settings, get_settings
from agents_system.execution_limits import (
    check_effective_limits,
    check_margin_covers_checkpointer,
)
from agents_system.harness.loader import (
    DefinitionError,
    RootConfig,
)
from agents_system.harness.registry import RegistryFactory
from agents_system.integration import openai_router, webhook_router
from agents_system.integration.operator_api import SubjectThreads
from agents_system.integration.operator_api import router as operator_router
from agents_system.integration.request_context import (
    DEFAULT_ADAPTER_CHANNEL,
    RequestContextHook,
)
from agents_system.integration.whatsapp_client import WhatsAppClient
from agents_system.models.base import get_bi_engine, get_engine, get_session_factory
from agents_system.observability import RequestIdMiddleware, setup_logging
from agents_system.observability.metrics import (
    DEFAULT_METRICS,
    DEFAULT_REGISTRY,
)
from agents_system.scope import ScopeProvider
from agents_system.services.checkpointer import CheckpointThreads
from agents_system.subject_data import (
    DEFAULT_REGISTRY as DEFAULT_SUBJECT_DATA,
)
from agents_system.subject_data import (
    SubjectDataPurger,
    SubjectDataRegistry,
)
from agents_system.usage import (
    BudgetCaps,
    parse_budgets,
)
from agents_system.user_memory import (
    limits_from_settings,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

from agents_system.services.admission import TurnAdmissionLimiter
from agents_system.services.outbox import (
    DEFAULT_LEASE_DURATION,
)
from agents_system.services.participants import (
    ChannelParticipantDirectory,
    ConversationRecorder,
    ParticipantDirectory,
    SubjectResolver,
)
from agents_system.services.redis import close_redis_pool, get_redis_client
from agents_system.services.retry_policy import OutboxRetryPolicy

__all__ = [
    "ProviderHealthProbe",
    "create_app",
    "lifespan",
    "verify_metrics_access",
]


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
        app.state.engine = get_engine(settings.database_url, settings)
        resource_stack.push_async_callback(app.state.engine.dispose)
        scope_provider = await _start_scope_provider(
            getattr(app.state, "scope_provider_factory", None), app.state.engine
        )
        _bind_proactive_store(app.state.subject_data_registry, scope_provider)

        budgets: dict[str, BudgetCaps] = parse_budgets(
            app.state.budgets
            if getattr(app.state, "budgets", None) is not None
            else settings.user_budgets
        )
        if app.state.subject_data_registry is DEFAULT_SUBJECT_DATA:
            # The process-wide registry is shared by every app; this app's
            # ledger goes into its own copy, so no other app (or test) sees it.
            app.state.subject_data_registry = DEFAULT_SUBJECT_DATA.copy()
        usage_ledger = _build_usage_ledger(
            scope_provider, app.state.subject_data_registry, budgets
        )
        app.state.usage_ledger = usage_ledger
        _bind_user_fact_store(scope_provider, app.state.subject_data_registry)
        _bind_schedule_store(scope_provider, app.state.subject_data_registry)
        user_memory = (
            limits_from_settings(settings) if scope_provider is not None else None
        )

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
        # Issue #166 -- the registration is process-wide, so it ends with
        # this lifespan: pushed first, it runs after `stop()` below (LIFO),
        # and a stopped sink is never left taking the process's events.
        resource_stack.callback(AuditSink.clear_current, audit_sink)
        resource_stack.push_async_callback(audit_sink.stop)

        # D-014 — outbound WhatsApp client (design AD-2), built once and shared.
        # #22 -- WhatsApp's settings are read through the channel's own view.
        whatsapp = WhatsAppChannelSettings.from_settings(settings)
        app.state.whatsapp_client = WhatsAppClient(
            httpx.AsyncClient(),
            phone_number_id=whatsapp.phone_number_id,
            token=whatsapp.access_token,
            base_url=whatsapp.graph_api_url,
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
            from agents_system.agent.limits import _effective_limits
            from agents_system.agent.runtime import AgentRuntime
            from agents_system.harness.factory import build_runtime
            from agents_system.harness.loader import resolve
            from agents_system.services.embeddings import (
                UnavailableEmbeddingProvider,
                get_embedding_provider,
            )

            # Built here, at boot, when the `embeddings` extra is installed:
            # the local model loads now, never inside a turn. Without the
            # extra the boot goes on (issue #188): the registry factory may
            # never use the embedder, so only a tool that does fails, with the
            # error naming the extra.
            try:
                embedder = get_embedding_provider(settings)
            except MissingExtraError as error:
                if error.extra != "embeddings":
                    raise
                _logger.warning(
                    "embeddings.unavailable",
                    extra=error.extra,
                    message=(
                        f"{error} Booting anyway: a tool that embeds text "
                        "fails with this error when it is called."
                    ),
                )
                embedder = UnavailableEmbeddingProvider(error)

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
                # Built through get_bi_engine, so it gets pool_pre_ping and
                # the separately configured BI capacity limits. This is the
                # engine most likely to sit behind a connection its pool has
                # held idle — a separate, possibly remote, read-only replica.
                candidate = get_bi_engine(settings.bi_database_url, settings)
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
            _refuse_builtin_default_tools(registry)

            session_provider = async_sessionmaker(
                app.state.engine, expire_on_commit=False
            )

            # Build the shared, global default chat model from
            # ADAPTER_PROVIDER -- unless the caller overrode it via
            # create_app(chat_model=...) (#83, Phase 3): a load scenario's
            # fast, deterministic fake model then powers every runtime that
            # has no per-agent override, instead of a real provider having to
            # stay reachable, and correct, under concurrent load. D-037
            # (issue #13, ADR-004) -- this stays the DEFAULT every runtime
            # falls back to; a runtime whose own resolved definition declares
            # its own `model:` still gets a dedicated one instead, built
            # per-runtime below (`_resolve_model_for_definition`), taking
            # precedence over this override too -- an explicit per-agent
            # choice is more specific than a load scenario's process-wide one.
            chat_model_override = getattr(app.state, "chat_model_override", None)
            default_model = (
                chat_model_override
                if chat_model_override is not None
                else _build_chat_model(settings.adapter_provider)
            )

            # D-014 S4 (design AD-1/AD-7) — shared checkpointer, only built
            # when checkpointing is enabled at all. Injected into every
            # AgentRuntime; each run_turn call still opts in per-invocation
            # via thread_id (design AD-1).
            checkpointer = None
            if settings.whatsapp_checkpointer_enabled:
                if settings.checkpointer_ttl_s is not None:
                    _logger.warning(
                        "checkpointer.deprecated_setting",
                        setting="CHECKPOINTER_TTL_S",
                        replacement="CHECKPOINTER_RETENTION_S",
                        retention_s=settings.checkpointer_retention_s,
                    )
                checkpointer = await resource_stack.enter_async_context(
                    _build_checkpointer_cm(settings)
                )
                # Issue #24 -- retention is enforced here, apart from the
                # saver: pushed after the checkpointer's own exit, so it is
                # stopped (LIFO) before the pool it purges through closes.
                purger = _build_checkpoint_purger(checkpointer, settings)
                if purger is not None:
                    await purger.start()
                    resource_stack.push_async_callback(purger.stop)

            # ADR-011 part 6 (#257): the operator routes delete and export a
            # subject's conversation threads through the same saver.
            app.state.checkpoint_threads = (
                CheckpointThreads(checkpointer) if checkpointer is not None else None
            )
            # One purger enforces every per-user store's retention. A name in
            # SUBJECT_DATA_RETENTION that no store registered fails the boot.
            # The facts' retention also reaches subjects who never return: the
            # facts store purges every subject's expired facts.
            purged_retention = {
                name: seconds
                for name, seconds in settings.subject_data_retention.items()
                if name != "user_facts"
                or "user_facts" in app.state.subject_data_registry.stores
            }
            if purged_retention:
                subject_purger = SubjectDataPurger(
                    app.state.subject_data_registry,
                    retention=purged_retention,
                    interval_s=settings.subject_data_purge_interval_s,
                )
                await subject_purger.start()
                resource_stack.push_async_callback(subject_purger.stop)

            roots: RootConfig | None = getattr(app.state, "roots", None)

            # #22 -- every webhook channel bound to each runtime id: the
            # built-in WhatsApp channel (WHATSAPP_RUNTIME_ID) and every
            # channel create_app(channels=...) registered. Each gets the same
            # boot-time checks below.
            channels_by_runtime: dict[str, list[str]] = {}
            if whatsapp.runtime_id:
                channels_by_runtime[whatsapp.runtime_id] = [WHATSAPP_CHANNEL]
            for channel_name, channel in _registered_channels(app).items():
                channels_by_runtime.setdefault(channel.runtime_id, []).append(
                    channel_name
                )

            # #278 -- the lease margin must hold a channel turn's thread read
            # and save, and its reply send.
            if channels_by_runtime and checkpointer is not None:
                check_margin_covers_checkpointer(settings.checkpointer_timeout_s)

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
                # #278 -- the deployment's ceiling, already checked by
                # `_boot_plan`; the definition may declare up to it.
                deployment_limits = plan.execution_limits.get(runtime_id)
                definition = resolve(
                    registration.locator,
                    client=client,
                    roots=roots,
                    execution_limits=deployment_limits,
                )

                # issue #128 item 1 -- a client named explicitly, whose
                # deployment override folder does not exist, must fail boot
                # rather than silently fall back to the generic,
                # un-narrowed role. `resolve()` sets `AgentDefinition.
                # deployment` to None on EXACTLY the no-override path (see
                # its own "No override" branch), so `client is not None`
                # together with `deployment is None` here means
                # `loader.load_override` already logged
                # `loader.override_not_found` and returned None -- a typo'd
                # or removed client folder, not a legitimate "this role has
                # no override" (that case never sets `client`). A deployment
                # override may only NARROW the generic role, so silently
                # falling back to it WIDENS the tool surface -- refuse
                # instead.
                if client is not None and definition.deployment is None:
                    raise DefinitionError(
                        f"Runtime {runtime_id!r} ({registration.subject}) "
                        f"specifies client override {client!r}, but its "
                        "deployment override folder does not exist (see the "
                        "loader.override_not_found warning above). A "
                        "missing override silently falls back to the "
                        "generic, un-narrowed role, widening the tool "
                        "surface -- refusing to boot. Fix the client name "
                        "or add the deployment folder."
                    )

                if deployment_limits:
                    # #278 -- every runtime the deployment set limits for,
                    # channel-bound or not: the combination is what runs.
                    check_effective_limits(
                        _effective_limits(
                            getattr(definition, "execution_limits", None),
                            deployment_limits,
                        ),
                        who=f"runtime {runtime_id!r}",
                        lease_s=DEFAULT_LEASE_DURATION.total_seconds(),
                    )
                _warn_if_turn_budget_below_request_timeout(
                    runtime_id, definition, settings, deployment_limits
                )

                # ADR-002 C.13 — a channel must only ever be bound to a role
                # marked safe for the input it delivers. Every webhook channel
                # (WhatsApp, and since #22 any registered one) is
                # attacker-reachable external input; check the resolved
                # definition BEFORE build_runtime does any work and BEFORE
                # app.state.runtimes is populated, so a misconfigured
                # deployment (e.g. an untrusted_input=false role bound to
                # WhatsApp) is a boot failure, not a per-message surprise
                # caught (or missed) later. Scoped to the channel-bound
                # runtime ids only — the OpenAI adapter is an accepted risk,
                # not enforced here (see ADR-002 C.13 "OpenAI adapter").
                #
                # A channel turn must also finish before its deferred-webhook
                # lease expires. Reserve explicit non-turn headroom for
                # surrounding persistence and provider-send work;
                # adapter-only runtimes do not run under this lease and
                # remain unconstrained by it.
                for channel_name in channels_by_runtime.get(runtime_id, ()):
                    _check_channel_runtime(
                        channel_name,
                        runtime_id,
                        registration.subject,
                        definition,
                        deployment_limits,
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
                #
                # issue #128 item 3 -- `definition=definition` reuses the
                # SAME resolved definition the untrusted_input/client checks
                # above already validated, instead of letting build_runtime
                # resolve role_type/client/roots a second, independent time.
                equipped = build_runtime(
                    role_type=registration.locator,
                    registry=registry,
                    granted_permissions=plan.grants[runtime_id],
                    client=client,
                    roots=roots,
                    session_provider=session_provider,
                    definition=definition,
                    # Issue #168 -- the in-process escalation default reaches
                    # no human. A served role without its own
                    # escalation_notifier fails boot, as it always has.
                    builtin_defaults=False,
                    execution_limits=deployment_limits,
                )
                # D-037 (issue #13, ADR-004) -- this runtime's own model:
                # `default_model` unless `definition.model` names its own
                # provider+model, in which case a dedicated one is built
                # here, at boot -- never lazily at first request.
                runtime_model = _resolve_model_for_definition(
                    definition, settings, default_model
                )
                # #78 Phase 0 Slice 2 -- runtime_id=runtime_id labels this
                # runtime's metrics with its registered runtime id (the
                # opaque key from create_app(agents=...) or
                # AGENT_REGISTRATIONS) instead of AgentRuntime's own derived
                # provider-model-id default (see AgentRuntime's `runtime_id`
                # parameter docstring in agent/graph.py).
                #
                # Issue #166 -- model_prices: the served app prices every turn
                # from MODEL_PRICES, handed to the runtime here. The runtime
                # itself never reads Settings, so library use needs no server
                # environment.
                runtimes[runtime_id] = AgentRuntime(
                    runtime=equipped,
                    model=runtime_model,
                    checkpointer=checkpointer,
                    runtime_id=runtime_id,
                    model_prices=settings.model_prices,
                    # Issue #24 -- the pool's statement_timeout is enforced by
                    # the server; this bounds each thread read and save on the
                    # client, so a database that stops answering cannot hold
                    # the turn, its admission slot or its outbox lease.
                    checkpointer_timeout_s=settings.checkpointer_timeout_s,
                    scope_provider=scope_provider,
                    # ADR-011 part 5 (#252): per-user caps of this runtime id,
                    # from the deployment only; never from the definition.
                    budget=budgets.get(runtime_id),
                    usage_ledger=usage_ledger,
                    # #250: facts about each end user, for a definition whose
                    # memory_policy opts in; the limits are the deployment's.
                    user_memory=user_memory,
                    # #278: the deployment's text for a timed-out turn.
                    timeout_reply=plan.timeout_replies.get(runtime_id),
                    # #289: the deployment's text for a turn that ends at
                    # max_tool_errors.
                    tool_error_reply=plan.tool_error_replies.get(runtime_id),
                )
                _logger.info("adapter.runtime_cached", model_id=runtime_id)

            unknown_budgets = sorted(set(budgets) - set(runtimes))
            if unknown_budgets:
                raise ValueError(
                    "budgets name runtime id(s) that are not built: "
                    + ", ".join(unknown_budgets)
                )

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
            app.state.runtimes.get(whatsapp.runtime_id) if whatsapp.runtime_id else None
        )
        if (
            webhook_runtime is None
            and whatsapp.access_token
            and whatsapp.phone_number_id
        ):
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
                f"but whatsapp_runtime_id={whatsapp.runtime_id!r} "
                "resolves to no runtime (unset). "
                "POST /webhook would durably accept signed WhatsApp messages "
                "that the deferred worker would never process. Set "
                "WHATSAPP_RUNTIME_ID to a valid runtime id, or unset "
                "WHATSAPP_TOKEN/WHATSAPP_PHONE_NUMBER_ID if this deployment "
                "does not use WhatsApp. Refusing to boot."
            )

        # #22 -- one worker serves every channel: WhatsApp through
        # `runtime`/`whatsapp_client` (unchanged), each channel
        # create_app(channels=...) registered through `channels`, on the
        # runtime its registration names (built and checked above).
        registered_channels = _registered_channels(app)
        if webhook_runtime is not None or registered_channels:
            from agents_system.services.webhook_worker import (
                ChannelBinding,
                DeferredWebhookWorker,
            )

            webhook_worker = DeferredWebhookWorker(
                session_factory=get_session_factory(app.state.engine),
                worker_id=f"webhook-worker-{uuid.uuid4()}",
                directory=app.state.participant_directory,
                runtime=webhook_runtime,
                whatsapp_client=(
                    app.state.whatsapp_client if webhook_runtime is not None else None
                ),
                recorder=app.state.conversation_recorder,
                checkpointer_enabled=whatsapp.checkpointer_enabled,
                retry_policy=plan.retry_policies.get(whatsapp.runtime_id or ""),
                poll_interval_s=settings.webhook_worker_poll_interval_s,
                claim_limit=settings.webhook_worker_claim_limit,
                admission_limiter=app.state.turn_admission_limiter,
                channels={
                    name: ChannelBinding(
                        adapter=channel.adapter,
                        runtime=app.state.runtimes[channel.runtime_id],
                        retry_policy=plan.retry_policies.get(
                            channel.runtime_id, OutboxRetryPolicy()
                        ),
                    )
                    for name, channel in registered_channels.items()
                },
            )
            await webhook_worker.start()
            resource_stack.push_async_callback(webhook_worker.stop)
            app.state.webhook_worker = webhook_worker
        else:
            structlog.get_logger().warning(
                "webhook_worker.no_runtime_resolved",
                whatsapp_runtime_id=whatsapp.runtime_id,
                detail=(
                    "no runtime resolved for whatsapp_runtime_id; the "
                    "deferred webhook worker did not start. Durable inbound "
                    "work still accumulates and waits -- it is not dropped."
                ),
            )
            app.state.webhook_worker = None

        yield


"""A runtime id lands in URLs, every log line of its turns and /metrics
labels, so it is short by contract, not only safe by character class."""


def _resolve_model_for_definition(
    definition: Any, settings: Settings, default_model: Any
) -> Any:
    """The chat model ``definition``'s own runtime should use (D-037, issue
    #13, ADR-004): a dedicated one built from its ``model`` declaration, else
    the shared ``default_model``.

    The rule lives in `agent.models.resolve_model_for_definition`, the same
    function the library path (`AgentRuntime(equipped)`,
    `agents_system.build_agent_model`) runs; this passes the served builder
    (`_build_chat_model`), so credentials are still read exclusively from
    ``settings``/the process environment. ``settings`` is unused: the served
    builder reads the process-wide ``get_settings``, exactly as before.
    """
    return _models.resolve_model_for_definition(
        definition, default_model, build=_build_chat_model
    )


def create_app(
    *,
    registry_factory: RegistryFactory,
    agents: Mapping[str, Agent | str] | None = None,
    grants: Mapping[str, Sequence[str]] | None = None,
    clients: Mapping[str, str] | None = None,
    participant_directory: (
        ParticipantDirectory | ChannelParticipantDirectory | None
    ) = None,
    conversation_recorder: ConversationRecorder | None = None,
    subject_resolver: SubjectResolver | None = None,
    subject_data: SubjectDataRegistry | None = None,
    subject_threads: SubjectThreads | None = None,
    budgets: Mapping[str, BudgetCaps | Mapping[str, Any]] | None = None,
    execution_limits: Mapping[str, Mapping[str, Any]] | None = None,
    timeout_reply: Mapping[str, str] | None = None,
    tool_error_reply: Mapping[str, str] | None = None,
    outbox_retry: Mapping[str, OutboxRetryPolicy | Mapping[str, Any]] | None = None,
    scope_provider_factory: Callable[[AsyncEngine], ScopeProvider] | None = None,
    roots: RootConfig | None = None,
    title: str = "agents_system",
    chat_model: BaseChatModel | None = None,
    channels: Mapping[str, Channel] | None = None,
    request_context_hook: RequestContextHook | None = None,
    adapter_channel: str = DEFAULT_ADAPTER_CHANNEL,
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
        inbound route fails closed and runs no turn. A plain
        ``ParticipantDirectory`` is never told which channel an address came
        from, so it serves WhatsApp alone: registering any ``channels`` needs
        a ``ChannelParticipantDirectory``, or boot fails.
    conversation_recorder:
        Records completed turns. Absent means none are recorded.
    budgets:
        ADR-011 part 5 (#252): per-user caps by runtime id, e.g.
        ``{"support": {"daily_usd": 1.0, "monthly_tokens": 500000,
        "message": "..."}}``; falls back to ``USER_BUDGETS``. Set by the
        deployment only. Needs ``scope_provider_factory`` (the usage ledger
        lives behind the scope handle) and, for a USD cap, a ``MODEL_PRICES``
        entry for the runtime's model; boot fails otherwise.
    execution_limits:
        #278: per-runtime limits by runtime id, e.g. ``{"reports":
        {"total_execution_timeout_s": 240, "tool_call_timeout_s": 60,
        "max_tool_calls": 30, "max_tool_errors": 5}}``; falls back to ``EXECUTION_LIMITS``. Set by
        the deployment only. Each value replaces the platform default as the
        runtime's ceiling; the agent may tighten it, never exceed it. Boot
        fails, naming the runtime, when a turn plus ``LEASE_MARGIN_S`` does
        not fit the outbox lease (under 540 s for the 600 s lease), when a
        tool call is longer than its turn, or for a runtime it does not build.
    timeout_reply:
        #278: the text a timed-out turn answers, by runtime id; falls back to
        ``TURN_TIMEOUT_REPLY``, then to the default English text. Plain text,
        sent as given.
    tool_error_reply:
        #289: the text a turn answers when its tool errors reach the
        ``max_tool_errors`` limit (``execution_limits``, default 3), by
        runtime id; falls back to ``TOOL_ERROR_REPLY``, then to the default
        English text. Plain text, sent as given.
    outbox_retry:
        #290: how the channel outbox retries a runtime's failed messages, by
        runtime id: an ``OutboxRetryPolicy`` (or a mapping of its fields) with
        ``max_attempts`` (default 5), ``is_transient`` (which exceptions are
        worth another attempt; the default retries provider rate limits, 5xx,
        timeouts and network or database connection errors and nothing else)
        and ``fallback_reply`` (plain text sent to the user, once, when the
        message fails for good). Without an entry the runtime keeps the
        defaults, or the ``OUTBOX_MAX_ATTEMPTS`` / ``OUTBOX_FALLBACK_REPLY``
        values for it; an entry here replaces them whole. Boot fails for a
        runtime id it does not build.
    roots:
        Consumer-owned path configuration for platform and deployment roles.
        A client override (``clients[id]``, or an ``AGENT_REGISTRATIONS``
        value ``"{role}@{client}"``) requires an explicit RootConfig
        specifying deployments_root.
    title:
        OpenAPI title.
    chat_model:
        Override the chat model every registered ``AgentRuntime`` is built
        with, instead of the lifespan's own ``_build_chat_model(settings.
        adapter_provider)`` (#83, Phase 3). ``None`` (the default) keeps that
        original behaviour byte-for-byte -- every existing caller is
        unaffected. A load scenario passes a fast, deterministic, zero-
        network fake model here instead of depending on a real provider
        staying reachable, and correct, under concurrent load; this is a
        SEPARATE seam from ``registry_factory``'s own model argument
        (`evals.live_registry.build_live_registry_factory`), which only
        drives the reference-backend connectors' own LLM calls, never the
        turn-running ``AgentRuntime`` itself.
    channels:
        Channel name -> ``agents_system.channels.port.Channel(adapter=...,
        runtime_id=...)`` (#22): extra messaging channels beside the built-in
        WhatsApp one, served at ``/webhook/{name}`` by the same generic route
        and deferred worker. A name is 1-32 lowercase letters, digits, ``_``
        or ``-`` (starting with a letter), and ``whatsapp`` is reserved; a bad
        entry fails here. Each ``runtime_id`` must be registered (``agents``
        or ``AGENT_REGISTRATIONS``), resolve ``untrusted_input=True`` and fit
        the outbox lease, or boot fails -- the checks ``WHATSAPP_RUNTIME_ID``
        gets. ``None`` (the default) registers none.
    request_context_hook:
        #249: an async callable the OpenAI-compatible adapter awaits once per
        ``/v1/chat/completions`` request with an ``AdapterRequestInfo``
        (runtime id, ``adapter_channel``, request id, session id, resolved
        subject) and nothing client-chosen. The text it returns is attached
        to that turn as trusted context, after the agent's system prompt
        (``run_turn(trusted_context=...)``); ``None`` or blank attaches
        nothing. A hook that raises, or returns anything but text or
        ``None``, fails the request with a 500 and runs no turn. ``None``
        (the default) keeps the adapter exactly as it was.
    adapter_channel:
        #249: the channel label the adapter hands ``request_context_hook``,
        a non-empty string; ``"openai-adapter"`` by default.
    """
    if request_context_hook is not None and not callable(request_context_hook):
        raise TypeError("request_context_hook must be an async callable or None")
    if not isinstance(adapter_channel, str) or not adapter_channel:
        raise ValueError("adapter_channel must be a non-empty string")
    setup_logging()
    registered_channels = _validate_channels(channels)

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
    application.state.subject_resolver = subject_resolver
    application.state.request_context_hook = request_context_hook
    application.state.adapter_channel = adapter_channel
    application.state.subject_data_registry = (
        subject_data if subject_data is not None else DEFAULT_SUBJECT_DATA
    )
    application.state.subject_threads = subject_threads
    application.state.budgets = budgets
    application.state.execution_limits = execution_limits
    application.state.timeout_reply = timeout_reply
    application.state.tool_error_reply = tool_error_reply
    application.state.outbox_retry = outbox_retry
    application.state.usage_ledger = None
    application.state.checkpoint_threads = None
    application.state.scope_provider_factory = scope_provider_factory
    application.state.roots = roots
    application.state.chat_model_override = chat_model
    application.state.provider_probe = ProviderHealthProbe()
    # #22 -- the generic webhook route resolves its adapter from here:
    # WhatsApp is built per request from that request's Settings (so a
    # dependency override reaches it, as it always has); a registered
    # channel brings its own adapter and configuration.
    application.state.channels = registered_channels
    resolvers: dict[str, ChannelResolver] = {
        WHATSAPP_CHANNEL: WhatsAppChannel.from_settings
    }
    for name, channel in registered_channels.items():
        resolvers[name] = _fixed_resolver(channel)
    application.state.inbound_channels = InboundChannels(
        default=WHATSAPP_CHANNEL, resolvers=resolvers
    )

    application.add_middleware(RequestIdMiddleware)
    application.include_router(webhook_router)
    application.include_router(openai_router)
    # ADR-011 part 6: no operator key, no operator routes (fail closed).
    if get_settings().operator_api_key:
        application.include_router(operator_router)

    logger = structlog.get_logger()

    async def health_report(
        request: Request,
        settings: Settings,
    ) -> dict[str, Any]:
        engine = request.app.state.engine
        redis_client = get_redis_client(settings.redis_url)

        postgres_status: Literal["ok", "error"] = "ok"
        redis_status: Literal["ok", "error"] = "ok"

        # A deployment without a runtime has no model dependency. Avoid a
        # meaningless network probe in that case (including offline tests that
        # deliberately build the app without its lifespan).
        runtimes = getattr(request.app.state, "runtimes", {})
        provider_reachable: bool | None = (
            await request.app.state.provider_probe.probe(settings) if runtimes else None
        )

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
            and provider_reachable is not False
            and not backlog_undrained
            else "degraded"
        )

        audit_sink = getattr(request.app.state, "audit_sink", None)
        audit_dropped_count = getattr(audit_sink, "dropped_count", 0)

        return {
            "status": overall,
            "environment": settings.environment,
            "postgres": postgres_status,
            "redis": redis_status,
            "provider": {
                "status": (
                    "not_configured"
                    if provider_reachable is None
                    else "ok"
                    if provider_reachable
                    else "error"
                ),
                "reason": None if provider_reachable is not False else "unreachable",
            },
            "audit": {"dropped_count": audit_dropped_count},
            "webhook_worker": {
                "running": worker_running,
                "outbox_pending": outbox_pending,
                "outbox_leased": outbox_leased,
                "outbox_leased_expired": outbox_leased_expired,
            },
        }

    @application.get("/health")
    async def health(
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> dict[str, Any]:
        """Liveness endpoint: always returns HTTP 200 with component status."""
        return await health_report(request, settings)

    @application.get("/ready")
    async def ready(
        request: Request,
        settings: Settings = Depends(get_settings),
    ) -> JSONResponse:
        """Readiness endpoint: returns 503 when a required dependency is degraded."""
        report = await health_report(request, settings)
        return JSONResponse(
            content=report, status_code=200 if report["status"] == "ok" else 503
        )

    @application.get("/metrics", dependencies=[Depends(verify_metrics_access)])
    async def metrics(request: Request) -> Response:
        """Prometheus text exposition of the process-wide metrics registry
        (#78 Phase 0 Slice 2). Access is gated by `verify_metrics_access`
        above -- absent (404) unless `metrics_enabled` is set, then bearer-
        protected exactly like `integration/openai_adapter.py`'s
        `verify_bearer` whenever `metrics_api_key` is configured.
        """
        audit_sink = getattr(request.app.state, "audit_sink", None)
        DEFAULT_METRICS.audit_events_dropped.set(
            getattr(audit_sink, "dropped_count", 0)
        )
        return Response(
            content=generate_latest(DEFAULT_REGISTRY),
            media_type=CONTENT_TYPE_LATEST,
        )

    return application
