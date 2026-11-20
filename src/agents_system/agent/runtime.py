"""AgentRuntime: the provider-agnostic multi-turn agent loop built on the agent graph."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Mapping, Sequence
from typing import Any

import structlog
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    ToolMessage,
)
from langgraph.checkpoint.base import BaseCheckpointSaver

from agents_system.agent.builder import (
    _build_graph,
)
from agents_system.agent.limits import (
    _effective_limits,
    _tighter,
)
from agents_system.agent.models import build_agent_model, declared_model
from agents_system.agent.nodes.model import (
    BudgetGate,
    _close_unanswered_tool_calls,
)
from agents_system.agent.state import AgentState
from agents_system.agent.thread import (
    DEFAULT_CHECKPOINTER_TIMEOUT_S,
    _run_tracking_progress,
    _Thread,
    _TurnProgress,
)
from agents_system.agent.untrusted import (
    DEFAULT_TURN_MAX_CHARS,
    UntrustedData,
    render_untrusted_blocks,
)
from agents_system.agent.usage import (
    TurnResult,
    TurnUsage,
    _aggregate_turn_usage,
    _compute_turn_cost,
    model_display_name,
)
from agents_system.config import ModelPrice
from agents_system.execution_limits import (
    DEFAULT_TIMEOUT_REPLY,
    DEFAULT_TOOL_ERROR_REPLY,
    DEPLOYMENT_LIMIT_KEYS,
    check_effective_limits,
    validate_execution_limits,
    validate_timeout_reply,
    validate_tool_error_reply,
)
from agents_system.harness.factory import EquippedRuntime
from agents_system.harness.injector import _emit
from agents_system.memory_policy import allows_user_memory, persists_conversation
from agents_system.observability.metrics import (
    DEFAULT_METRICS,
    Metrics,
    record_turn,
)
from agents_system.permissions import UnknownPermissionNameError, covers
from agents_system.permissions.permission_registry import permission_registry
from agents_system.scope import ScopeProvider
from agents_system.subject import (
    TurnActor,
    TurnSubject,
    current_actor,
    current_subject,
    turn_scope,
)
from agents_system.usage import (
    BudgetCaps,
    UsageLedger,
    UsageRecord,
    UsageTotals,
    reached_cap,
)
from agents_system.user_memory import UserFacts, UserMemoryLimits, backend_for

logger = structlog.get_logger()


def _subject_key() -> str | None:
    subject = current_subject()
    return subject.key if subject is not None else None


def _actor_kind() -> str:
    actor = current_actor()
    return actor.kind if actor is not None else "none"


def _count_tool_messages(messages: Sequence[AnyMessage]) -> int:
    return sum(1 for m in messages if isinstance(m, ToolMessage))


def _actor_label() -> str | None:
    actor = current_actor()
    return actor.label if actor is not None else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: LangChain integration package -> the declarable ``provider`` its chat model
#: classes serve. ``langchain_openai`` covers every OpenAI-protocol host, which
#: is exactly what ``openai_compatible`` declares; the package cannot tell one
#: such host from another, so that is the limit of the comparison.
_PROVIDER_BY_PACKAGE = {
    "langchain_openai": "openai_compatible",
    "langchain_groq": "groq",
    "langchain_anthropic": "anthropic",
    "langchain_ollama": "ollama",
}


def _model_provider(model: BaseChatModel) -> str | None:
    """The declarable provider ``model`` belongs to, or ``None`` if unknown.

    Read from the package that defines the model's class or any of its bases
    (so a subclass such as ``ReasoningSanitizedChatOpenAI`` is still
    recognised). A test double, a custom wrapper or a class from another
    package is unidentifiable: ``None``, never a guess.
    """
    for cls in type(model).__mro__:
        package = cls.__module__.split(".", 1)[0]
        if package in _PROVIDER_BY_PACKAGE:
            return _PROVIDER_BY_PACKAGE[package]
    return None


def _report_model_conflict(definition: Any, model: BaseChatModel) -> None:
    """Warn when the caller's ``model`` is not the one the agent declares.

    Issue #186. The caller's model always wins (see ``AgentRuntime``'s
    ``model`` parameter); this only makes the disagreement visible, so a
    declaration that is not being honored is never silent. Compared by the
    provider model id (`model_display_name`) AND the provider, because two
    providers can serve the same id string. A declared temperature or
    ``max_tokens`` is not comparable on an arbitrary model object, so it is
    not checked. Nothing is built here: the declared model is never
    constructed just to be compared.

    Limits of the comparison: the provider is read from the model's class
    (`_model_provider`), so it separates ``openai_compatible`` / ``groq`` /
    ``anthropic`` / ``ollama`` but not two OpenAI-protocol hosts from each
    other. When the id matches and the provider cannot be identified, the
    models are not called equivalent: ``runtime.model_provider_unverified``
    (info, not a conflict) says the provider could not be established.
    """
    declared = declared_model(definition)
    if declared is None:
        return
    agent = getattr(definition, "role_name", "<unknown>")
    declared_id = declared["model"]
    declared_provider = declared["provider"]
    passed_id = model_display_name(model)
    if passed_id == declared_id:
        passed_provider = _model_provider(model)
        if passed_provider == declared_provider:
            return
        if passed_provider is None:
            logger.info(
                "runtime.model_provider_unverified",
                agent=agent,
                model=declared_id,
                declared_provider=declared_provider,
                detail=(
                    "the passed model reports the declared model id but its "
                    "provider cannot be identified, so it is not known to be "
                    "the declared model; the passed model is used."
                ),
            )
            return
    else:
        passed_provider = _model_provider(model)
    logger.warning(
        "runtime.model_conflict",
        agent=agent,
        declared_model=declared_id,
        passed_model=passed_id,
        declared_provider=declared_provider,
        passed_provider=passed_provider,
        detail=(
            "the agent declares a model but AgentRuntime was given a different "
            "one; the passed model is used and the declaration is ignored. "
            "Drop model= to run on the declared model."
        ),
    )


class AgentRuntime:
    """Provider-agnostic, async-native agent runtime.

    Parameters
    ----------
    runtime:
        The fully assembled EquippedRuntime (tools surface, system prompt, etc.).
    model:
        Any LangChain BaseChatModel instance. The runtime binds the granted tool
        surface to it at construction time. Swapping providers requires only
        changing this argument — no other runtime code changes.

        Issue #186 -- optional. Omitted (``None``), the runtime builds the
        model the agent DECLARES (``Agent(model={...})``, or ``model:`` in its
        frontmatter, inherited through ``extends``) with
        ``agents_system.build_agent_model``, reading credentials only from the
        process environment and ``ProviderSettings`` -- never from the server
        ``Settings``. An agent that declares none raises ``ValueError``.
        Passed AND different from the declared model id, the passed model wins
        and a ``runtime.model_conflict`` warning names both: the caller's
        explicit argument is never silently overridden by configuration, and
        the declaration is never silently inert.
    checkpointer:
        Optional shared LangGraph checkpointer (design AD-1). Only engaged
        when a ``thread_id`` is also passed to a given ``run_turn`` call —
        entry points with their own full-history contract (e.g. the OpenAI
        adapter) never pass a ``thread_id`` and stay byte-identical to the
        pre-D-014 stateless behavior. Any LangGraph saver works; the served
        app passes the Postgres one (issue #24). The thread is read before
        the turn and saved once after it, and a checkpointer that fails
        degrades the turn instead of failing it (see ``_Thread``).
    checkpointer_timeout_s:
        Issue #24 -- the most seconds one thread read or one save may take
        before it is abandoned and handled as a failed read or save, so a
        database that stops answering cannot hold a turn. Enforced on the
        client, for any saver. Defaults to
        ``DEFAULT_CHECKPOINTER_TIMEOUT_S`` (5 seconds); the served app
        passes ``CHECKPOINTER_TIMEOUT_S``. Must be positive.
    runtime_id:
        #78 Phase 0 Slice 2 -- the bounded, low-cardinality ``runtime_id``
        label this runtime's metrics are recorded under (see
        ``observability/metrics.py``'s module docstring on label hygiene).
        ``None`` (default) falls back to this runtime's own derived provider
        model id (``model_display_name(model)``, the same default
        ``run_turn``'s ``model_id`` pricing override already uses) — the
        only caller that has a more meaningful id to give (the registered
        runtime id: the opaque key from ``create_app(agents=...)`` or
        ``AGENT_REGISTRATIONS``) is ``main.py``'s lifespan, which passes it
        explicitly; every other existing construction site
        (``evals/runner.py``, direct library use) is unaffected.
    metrics:
        #78 Phase 0 Slice 2 -- the ``Metrics`` set this runtime's turns,
        tool calls and limit trips are recorded into. ``None`` (default)
        uses the process-wide ``DEFAULT_METRICS`` (the set ``GET /metrics``
        serves); a test passes its own throw-away set built from a fresh
        ``CollectorRegistry`` instead, so recorded values are isolated and
        assertable without touching process-wide state.
    model_prices:
        Issue #166 -- the price table each turn's ``TurnUsage.cost_usd`` is
        computed from, keyed by provider model id (see ``run_turn``'s
        ``model_id``). ``None`` (default) means no prices: every turn's cost
        is ``None``, never a guessed number. ``create_app``'s lifespan fills
        it from ``MODEL_PRICES`` (``Settings.model_prices``); a library user
        passes their own, or nothing. The runtime never reads the server
        ``Settings`` itself -- they fail closed without the server's
        environment.
    budget:
        ADR-011 part 5 (#252) -- per-user caps (`BudgetCaps`) for this runtime,
        set by the deployment; an agent definition cannot raise them. A turn
        whose subject has reached a cap is refused before any model call, and
        the cap is checked again before each later call of the turn. Needs a
        ``usage_ledger``; a USD cap needs a price for the runtime's model.
    usage_ledger:
        Where each turn's usage is recorded, per (runtime id, subject key). A
        turn with no subject is not recorded (it counts against the runtime
        only, in metrics). A failed write is logged and never fails the turn.
    user_memory:
        #250 -- the limits of per-user memory. With them, a turn that has a
        subject, on a definition whose ``memory_policy`` says
        ``user_memory: true``, gets the user's stored facts as an
        untrusted-data block (never as plain text). Needs ``scope_provider``
        and ``runtime_id``. A store that fails is logged and the turn goes on
        without the facts.
    execution_limits:
        #278 -- the deployment's ceiling for this runtime
        (``total_execution_timeout_s``, ``tool_call_timeout_s``,
        ``max_tool_calls``), like ``build_runtime(execution_limits=...)``;
        given to both, the tighter value of each key wins. It replaces the
        platform default as the ceiling: the definition's own value may
        tighten it, never exceed it. A turn must fit the outbox lease with
        ``LEASE_MARGIN_S`` to spare and a tool call must fit its turn, or
        construction raises ``ValueError`` naming the runtime. The effective
        limits are logged (``runtime.execution_limits``).
    timeout_reply:
        #278 -- the text a timed-out turn answers, sent as given (never
        templated with user or tool data). ``None``: ``DEFAULT_TIMEOUT_REPLY``.
    tool_error_reply:
        #289 -- the text a turn answers when its tool errors reach
        ``max_tool_errors`` (a limit like the others in ``execution_limits``,
        default 3), sent as given (never templated). ``None``:
        ``DEFAULT_TOOL_ERROR_REPLY``.
    """

    def __init__(
        self,
        runtime: EquippedRuntime,
        model: BaseChatModel | None = None,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
        runtime_id: str | None = None,
        metrics: Metrics | None = None,
        model_prices: Mapping[str, ModelPrice] | None = None,
        checkpointer_timeout_s: float = DEFAULT_CHECKPOINTER_TIMEOUT_S,
        scope_provider: ScopeProvider | None = None,
        budget: BudgetCaps | None = None,
        usage_ledger: UsageLedger | None = None,
        user_memory: UserMemoryLimits | None = None,
        execution_limits: Mapping[str, Any] | None = None,
        timeout_reply: str | None = None,
        tool_error_reply: str | None = None,
    ) -> None:
        if user_memory is not None and (scope_provider is None or runtime_id is None):
            raise ValueError(
                "an AgentRuntime with user_memory needs a scope_provider to keep "
                "the facts in and an explicit runtime_id to key them by"
            )
        if budget is not None and usage_ledger is None:
            raise ValueError("a budget needs a usage_ledger to count usage in")
        if usage_ledger is not None and runtime_id is None:
            raise ValueError(
                "an AgentRuntime with a usage_ledger needs an explicit runtime_id; "
                "the default is the model name"
            )
        if scope_provider is not None and runtime_id is None:
            # ADR-011 part 1: per-user data is keyed by the runtime id, and
            # the fallback is the model's display name, which two agents on
            # one model share. Refuse rather than mix their users' data.
            raise ValueError(
                "an AgentRuntime with a scope_provider (any per-user store) "
                "needs an explicit runtime_id; the default is the model name"
            )
        if not checkpointer_timeout_s > 0:
            raise ValueError(
                f"checkpointer_timeout_s must be positive, got {checkpointer_timeout_s!r}"
            )
        if model is None:
            model = build_agent_model(runtime.definition)
        else:
            _report_model_conflict(runtime.definition, model)
        self._runtime = runtime
        self._equipped = runtime
        self._checkpointer = checkpointer
        self._checkpointer_timeout_s = float(checkpointer_timeout_s)
        self._schemas = [spec.to_langchain_tool_schema() for spec in runtime.tools]
        # Only call bind_tools when there are tools to bind — some fake models
        # raise NotImplementedError for bind_tools even with an empty list.
        self._bound_model = model.bind_tools(self._schemas) if self._schemas else model
        # #78 Phase 0 (review finding 4) -- derived from the ORIGINAL,
        # unbound `model` (bind_tools's return value, e.g. a RunnableBinding,
        # no longer exposes `model_name`/`model`). This runtime's own default
        # model_prices lookup key: a caller's `run_turn(model_id=...)`
        # is at most an optional override, never required for this runtime's
        # usage to be priced.
        self._model_id = model_display_name(model)
        # #78 Phase 0 Slice 2 -- see the `runtime_id`/`metrics` parameter
        # docs above.
        self._runtime_id = runtime_id if runtime_id is not None else self._model_id
        # #278 -- the deployment's ceiling, from build_runtime and from here.
        who = f"runtime {self._runtime_id!r}"
        self._deployment_limits = _tighter(
            validate_execution_limits(
                getattr(runtime, "deployment_limits", None), who=who
            ),
            validate_execution_limits(execution_limits, who=who),
        )
        self._timeout_reply = (
            DEFAULT_TIMEOUT_REPLY
            if timeout_reply is None
            else validate_timeout_reply(timeout_reply, who=who)
        )
        self._tool_error_reply = (
            DEFAULT_TOOL_ERROR_REPLY
            if tool_error_reply is None
            else validate_tool_error_reply(tool_error_reply, who=who)
        )
        effective = self.execution_limits
        if self._deployment_limits:
            # Each ceiling was valid alone; what runs is their combination
            # with the definition's own values.
            check_effective_limits(effective, who=who)
        logger.info(
            "runtime.execution_limits",
            runtime_id=self._runtime_id,
            **{key: effective[key] for key in DEPLOYMENT_LIMIT_KEYS},
            deployment_set=sorted(self._deployment_limits),
        )
        self._scope_provider = scope_provider
        self._metrics = metrics if metrics is not None else DEFAULT_METRICS
        # Issue #166 -- copied, so a caller mutating its own dict later cannot
        # reprice turns behind this runtime's back.
        self._model_prices: dict[str, ModelPrice] = dict(model_prices or {})
        if budget is not None:
            budget.validate(f"budget for runtime {self._runtime_id!r}")
        if (
            budget is not None
            and budget.has_usd_cap
            and self._model_id not in self._model_prices
        ):
            # A USD cap cannot be enforced without a price, and pricing by
            # guessing is never done: fail at boot, not at the first turn.
            raise ValueError(
                f"a USD budget for runtime {self._runtime_id!r} needs a price for "
                f"model {self._model_id!r} in model_prices (MODEL_PRICES)"
            )
        self._budget = budget
        self._usage_ledger = usage_ledger
        self._user_memory = user_memory
        logger.info(
            "runtime.initialized",
            tools=len(self._schemas),
            model_type=type(model).__name__,
        )
        # D-007: record runtime_initialized event
        _emit(
            "record_runtime_initialized",
            definition=self._equipped.definition,
            tools_count=len(self._schemas),
            model_type=type(model).__name__,
        )

    @property
    def execution_limits(self) -> dict[str, Any]:
        """The limits this runtime's turns run with: the definition's, over
        the deployment's ceiling (#278), over the platform defaults."""
        return _effective_limits(
            getattr(self._equipped.definition, "execution_limits", None),
            self._deployment_limits,
        )

    @property
    def permissions(self) -> tuple[str, ...]:
        """The role's own DECLARED permission set (``definition.permissions``).

        NOT the deploy-time grant — see ``EquippedRuntime.deploy_grant_ceiling``
        for what this runtime was actually granted (issue #38). A role
        merely declaring a permission is not the same as a deployment
        granting it; ``run_turn``'s own default no longer sources from this
        property (design.md Resolved Decision 5).
        """
        return self._equipped.definition.permissions

    @property
    def persist_conversation(self) -> bool:
        """Whether the definition's ``memory_policy`` lets a conversation be
        durably persisted (#26). False also when the policy does not parse.
        A live projection of the definition, like ``permissions``."""
        return persists_conversation(self._equipped.definition.memory_policy)

    @property
    def untrusted_input(self) -> bool:
        """Whether the resolved definition treats this runtime's input as
        untrusted (ADR-002 C.11). Sourced from the same `AgentDefinition`
        `permissions` reads above — a live projection, not a copy taken at
        construction time. `create_app`'s boot-time channel check (ADR-002
        C.13) reads this to refuse booting a channel bound to a role that is
        not marked safe for untrusted external input.
        """
        return self._equipped.definition.untrusted_input

    async def run_turn(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...] | None = None,
        thread_id: str | None = None,
        model_id: str | None = None,
        *,
        subject: TurnSubject | None = None,
        actor: TurnActor | None = None,
        untrusted_data: Sequence[UntrustedData] = (),
        trusted_context: str = "",
    ) -> list[AnyMessage]:
        """Execute one conversational turn; return only the message list.

        Delegates to `run_turn_with_usage` (issue #78 review finding 5) and
        returns its `.messages` -- kept for every existing caller that only
        ever needed a `list[AnyMessage]` (`evals/runner.py`'s call sites that
        don't read usage, `services/webhook_worker.py`, the wider test
        suite). A caller that also wants this turn's real `TurnUsage` calls
        `run_turn_with_usage` instead -- see its docstring for the full
        parameter reference, unchanged here.
        """
        result = await self.run_turn_with_usage(
            messages,
            session_id,
            permissions=permissions,
            thread_id=thread_id,
            model_id=model_id,
            subject=subject,
            actor=actor,
            untrusted_data=untrusted_data,
            trusted_context=trusted_context,
        )
        return result.messages

    async def run_turn_with_usage(
        self,
        messages: list[AnyMessage],
        session_id: str,
        permissions: tuple[str, ...] | None = None,
        thread_id: str | None = None,
        model_id: str | None = None,
        *,
        subject: TurnSubject | None = None,
        actor: TurnActor | None = None,
        untrusted_data: Sequence[UntrustedData] = (),
        trusted_context: str = "",
    ) -> TurnResult:
        """Execute one conversational turn.

        ``subject`` / ``actor`` (ADR-011) name the end user the turn is for
        and who started it. The entry point binds them, never the model; both
        default to ``None`` and a turn without them behaves as before. They
        are visible for the duration of the turn to audit events and turn
        logs (subject key only, never the address).

        By default the runtime is stateless: it does NOT persist messages
        between calls, and the caller supplies the full message history each
        turn. Passing ``thread_id`` opts THIS call into the shared checkpointer
        (design AD-1) — cross-turn history then accumulates server-side,
        keyed by ``thread_id``, and ``messages`` only needs to carry the new
        turn's message(s).

        Parameters
        ----------
        messages:
            Message(s) for this turn. Full history when stateless
            (``thread_id=None``); only the new turn's message(s) when a
            checkpointer is engaged (``thread_id`` given).
        session_id:
            Logical session identifier (passed through, stored in AgentState).
        permissions:
            The caller's current permission grants used by the Layer-2 interceptor
            to validate sensitive tool calls at execution time. Defaults to
            ``None``, in which case the equipped runtime's persisted deploy
            grant ceiling (``EquippedRuntime.deploy_grant_ceiling``, issue
            #38) is used — never the role's full declared permission set
            (``self.permissions``) — so Layer-2 cannot widen back out past
            what this runtime was actually granted at boot. This is the
            correct default for every entry point that has no separate
            identity of its own (OpenAI adapter, WhatsApp webhook).
        thread_id:
            Opt-in checkpointer key (design AD-1), e.g. the client's
            normalized phone number. ``None`` (default) keeps this call fully
            stateless — no checkpointer is engaged even if one is configured
            on this runtime. Ignored (treated as stateless) if this runtime
            was constructed without a ``checkpointer``.
        model_id:
            OPTIONAL OVERRIDE (issue #78 review finding 4) of the model id
            this turn's usage is priced under, looked up in this runtime's
            ``model_prices``. ``None`` (default) uses this
            runtime's OWN derived default instead --
            ``model_display_name(model)``, computed once from the provider
            model this ``AgentRuntime`` was constructed with (e.g.
            ``"gpt-4o"``, ``"deepseek/deepseek-v4-flash"``) -- so a caller
            that never names a model id (the WhatsApp webhook worker) still
            gets priced correctly, keyed by the actual provider model
            billing the tokens rather than a caller-chosen routing id. Pass
            an explicit value only to price under a different id than this
            runtime's own model (e.g. a live-eval comparing several runtime
            configurations under one label).
        untrusted_data:
            Issue #258 (ADR-011 part 4) -- stored or external data for this
            turn, the only way such data reaches the model. Rendered once per
            turn next to the system prompt, delimited by a random boundary,
            capped, counted against ``max_context_tokens`` and never
            persisted. Never policy: it changes no tool, grant or limit.
        trusted_context:
            Issue #249 -- text the APPLICATION built for this turn (the
            OpenAI-compatible adapter's request-context hook), never user
            content. Appended to the system message after the agent's own
            prompt, under a fixed header, so the prompt keeps priority;
            counted against ``max_context_tokens`` and never persisted, like
            the prompt itself. Empty (the default) leaves the prompt as is.

        Returns
        -------
        TurnResult
            ``messages``: a real ``list[AnyMessage]`` of every message
            accumulated during this turn (input + model responses + tool
            messages). ``usage``: this turn's real ``TurnUsage`` (tokens +
            cost, honestly ``None`` where unknown — see ``TurnUsage``'s
            docstring). When stateless, the caller owns cross-turn
            aggregation; when a checkpointer is engaged, the checkpointer
            owns cross-turn accumulation and ``messages`` already reflects
            it.
        """
        with turn_scope(subject, actor):
            return await self._run_turn_scoped(
                messages,
                session_id,
                permissions=permissions,
                thread_id=thread_id,
                model_id=model_id,
                untrusted_data=untrusted_data,
                trusted_context=trusted_context,
            )

    def _may_read_user_memory(self, permissions: Sequence[str] | None) -> bool:
        """Whether the deploy grant ceiling, narrowed by the turn, covers the read.

        The same coverage rule the interceptor applies to ``list_user_facts``:
        injecting the facts is a read of them, so a deployment that did not
        grant ``read:user_memory`` (or revoked it) gets no injection either.
        """
        granted = self._equipped.deploy_grant_ceiling
        if permissions is not None:
            current: set[type] = set()
            for name in permissions:
                try:
                    current.add(permission_registry.resolve(name))
                except UnknownPermissionNameError:
                    continue
            granted = granted & current
        needed = permission_registry.resolve("read:user_memory")
        return any(covers(cls, needed) for cls in granted)

    async def _user_memory_blocks(
        self, permissions: Sequence[str] | None = None
    ) -> list[UntrustedData]:
        """The turn's user-facts block, or none (#250).

        Only for a turn with a subject, on a definition that opted in, whose
        grants cover ``read:user_memory``. Never fails the turn: a store that
        cannot be read means a turn without the facts, logged.
        """
        subject = current_subject()
        if (
            self._user_memory is None
            or self._scope_provider is None
            or subject is None
            or not allows_user_memory(self._equipped.definition.memory_policy)
            or not self._may_read_user_memory(permissions)
        ):
            return []
        try:
            facts = UserFacts(
                backend_for(self._scope_provider.scope(self._runtime_id, subject.key)),
                self._user_memory,
            )
            block = await facts.injection_block(mark_used=True)
        except Exception:
            logger.warning("user_memory.inject_failed", exc_info=True)
            return []
        return [block] if block is not None else []

    async def _run_turn_scoped(
        self,
        messages: list[AnyMessage],
        session_id: str,
        *,
        permissions: tuple[str, ...] | None,
        thread_id: str | None,
        model_id: str | None,
        untrusted_data: Sequence[UntrustedData],
        trusted_context: str = "",
    ) -> TurnResult:
        """The turn itself, run inside `turn_scope`."""
        # #78 Phase 0 (review finding 4) -- an explicit override always wins;
        # otherwise fall back to this runtime's own derived provider model id.
        effective_model_id = model_id if model_id is not None else self._model_id
        effective_permissions = (
            permissions
            if permissions is not None
            # issue #38 — default to the persisted deploy grant ceiling, not
            # the role's full declared permission set (self.permissions):
            # Layer-2 must not be able to widen back out past what this
            # runtime was actually granted at boot.
            else tuple(
                sorted(
                    permission_registry.reverse(cls)
                    for cls in self._equipped.deploy_grant_ceiling
                )
            )
        )
        effective_limits = self.execution_limits
        max_tool_calls = effective_limits["max_tool_calls"]
        turn_start = time.monotonic()
        untrusted_data = [
            *untrusted_data,
            *await self._user_memory_blocks(effective_permissions),
        ]
        if (
            self._budget is not None
            and self._budget.has_usd_cap
            and effective_model_id not in self._model_prices
        ):
            # The boot guard only covers the runtime's own model; a per-turn
            # override without a price would make every cost null and the USD
            # cap unreachable. Refuse before any model call.
            raise ValueError(
                f"a USD budget for runtime {self._runtime_id!r} needs a price for "
                f"model {effective_model_id!r} in model_prices (MODEL_PRICES)"
            )
        gate = self._budget_gate(effective_model_id)
        if gate is not None:
            refusal = await gate(None)
            if refusal is not None:
                return self._finish_turn(
                    [*messages, AIMessage(content=refusal)],
                    usage=TurnUsage(0, 0, 0, 0, 0.0),
                    session_id=session_id,
                    model_id=effective_model_id,
                    outcome="budget_refused",
                    duration_s=time.monotonic() - turn_start,
                    budget_refused=True,
                )
        graph = _build_graph(
            self._equipped,
            self._bound_model,
            effective_permissions,
            max_tool_calls=max_tool_calls,
            tool_call_timeout_s=effective_limits["tool_call_timeout_s"],
            max_tool_errors=int(effective_limits["max_tool_errors"]),
            tool_error_reply=self._tool_error_reply,
            metrics=self._metrics,
            max_context_tokens=int(effective_limits["max_context_tokens"]),
            scope_provider=self._scope_provider,
            runtime_id=self._runtime_id,
            budget_gate=gate,
            # Never more than the context budget itself (about 4 characters a
            # token): an over-budget request is never built from the blocks.
            untrusted_text=render_untrusted_blocks(
                untrusted_data,
                total_max_chars=min(
                    DEFAULT_TURN_MAX_CHARS,
                    max(1, int(effective_limits["max_context_tokens"]) * 4),
                ),
            ),
            trusted_context=trusted_context,
        )
        # Issue #24 -- a thread's history is read here, before the turn, and
        # the turn is saved once at the end (see `_Thread` for the failure
        # policy). The graph itself always runs without a checkpointer.
        # Neither the read nor the save counts against the turn budget: the
        # budget bounds the agent's own work, and each of them has its own
        # bound, `checkpointer_timeout_s`.
        thread = (
            _Thread(
                graph.compile(checkpointer=self._checkpointer),
                thread_id,
                timeout_s=self._checkpointer_timeout_s,
                metrics=self._metrics,
            )
            if thread_id is not None
            and self._checkpointer is not None
            and self.persist_conversation
            else None
        )
        history = await thread.load() if thread is not None else []
        # D-014 S4 (design AD-1): the system prompt is no longer prepended
        # here — _call_model prepends it to the MODEL INPUT on every call and
        # never persists it in state (see _call_model docstring).
        all_messages = [*history, *messages]
        initial_state: AgentState = {
            "messages": all_messages,
            "session_id": session_id,
            "current_permissions": effective_permissions,
            # Always set explicitly (not inherited from a prior checkpoint):
            # a per-turn budget must reset to 0 even when this thread resumes
            # from persisted state (design AD-1 gotcha).
            "tool_call_count": 0,
            "tool_error_count": 0,
            # #78 Phase 0 -- same reset-per-turn reasoning as tool_call_count
            # above: a resumed checkpointed thread must never inherit a
            # prior turn's usage entries.
            "turn_usage": [],
        }
        # D-014 S2 (design AD-3): a hard recursion_limit backstop derived from
        # max_tool_calls. LangGraph's own default (25 super-steps) is too low
        # for a legitimately full-budget turn (roughly 2 steps per tool call
        # plus the final call_model step) — without this override a turn that
        # stays exactly within its own max_tool_calls budget could still crash
        # with GraphRecursionError instead of completing normally.
        recursion_limit = 2 * max_tool_calls + 10
        # #78 Phase 0 Slice 2 -- measured across the whole call (graph build
        # through the final _finish_turn below), the single choke point both
        # the "ok" and "timeout" outcomes already return through. The outer
        # `except Exception` records "error" for anything neither branch
        # below catches (e.g. a genuine provider failure) and then re-raises
        # unchanged -- this adds observability only, it does not change what
        # was already unhandled before this metric existed. A checkpointer
        # failure never reaches it: `_Thread` handles every one.
        progress = _TurnProgress()
        try:
            try:
                async with asyncio.timeout(
                    effective_limits["total_execution_timeout_s"]
                ):
                    result = await _run_tracking_progress(
                        graph.compile(),
                        initial_state,
                        {"recursion_limit": recursion_limit},
                        progress,
                    )
            except TimeoutError:
                logger.warning(
                    "runtime.timeout",
                    total_execution_timeout_s=effective_limits[
                        "total_execution_timeout_s"
                    ],
                )
                # D-007: record runtime_timeout event
                _emit(
                    "record_runtime_timeout",
                    definition=self._equipped.definition,
                    total_execution_timeout_s=effective_limits[
                        "total_execution_timeout_s"
                    ],
                )
                # Issue #172 -- the cancelled invocation returns nothing, but
                # `progress` holds the state after the last step that
                # completed: keep its messages (the tool calls already made)
                # and account for the model calls that returned. A request
                # still in flight reported no usage and is not counted (see
                # `TurnUsage`).
                partial = progress.state
                if partial is not None:
                    partial_messages = list(partial["messages"])
                    raw_partial_usage = partial.get("turn_usage", [])
                else:
                    partial_messages = all_messages
                    raw_partial_usage = []
                reason = (
                    "the turn timed out after "
                    f"{float(effective_limits['total_execution_timeout_s'])}s"
                )
                # #278 -- the deployment's text, as configured: never
                # formatted with anything from the turn.
                timeout_reply = AIMessage(content=self._timeout_reply)
                if thread is not None:
                    # Issue #24 -- the thread keeps the turn as the user got
                    # it: its interrupted tool calls answered, then the reply.
                    await thread.save(
                        _close_unanswered_tool_calls(
                            partial_messages[len(history) :], reason=reason
                        )
                        + [timeout_reply]
                    )
                timed_out_result = self._finish_turn(
                    _close_unanswered_tool_calls(partial_messages, reason=reason)
                    + [timeout_reply],
                    usage=_aggregate_turn_usage(raw_partial_usage),
                    session_id=session_id,
                    model_id=effective_model_id,
                    outcome="timeout",
                    duration_s=time.monotonic() - turn_start,
                    timed_out=True,
                    timeout_s=float(effective_limits["total_execution_timeout_s"]),
                )
                await self._record_usage(
                    timed_out_result,
                    effective_model_id,
                    _count_tool_messages(partial_messages[len(all_messages) :]),
                )
                return timed_out_result
        except Exception:
            record_turn(
                self._metrics,
                runtime_id=self._runtime_id,
                outcome="error",
                duration_s=time.monotonic() - turn_start,
            )
            raise
        final_messages = list(result["messages"])
        if thread is not None:
            await thread.save(final_messages[len(history) :])
        finished = self._finish_turn(
            final_messages,
            usage=_aggregate_turn_usage(result.get("turn_usage", [])),
            session_id=session_id,
            model_id=effective_model_id,
            outcome="budget_refused" if result.get("budget_refused") else "ok",
            duration_s=time.monotonic() - turn_start,
            budget_refused=bool(result.get("budget_refused")),
        )
        await self._record_usage(
            finished,
            effective_model_id,
            _count_tool_messages(final_messages[len(all_messages) :]),
        )
        return finished

    def _budget_gate(self, model_id: str | None) -> BudgetGate | None:
        """The per-user cap check for the running turn, or ``None`` without caps.

        Called with the graph state (``None`` before the graph ran); the usage
        the turn already made is added to the ledger's totals, because the
        ledger is only written when the turn ends.
        """
        caps, ledger, subject = self._budget, self._usage_ledger, current_subject()
        if caps is None or ledger is None or subject is None:
            return None
        runtime_id = self._runtime_id

        async def gate(state: AgentState | None) -> str | None:
            pending = UsageTotals()
            if state is not None:
                usage = _aggregate_turn_usage(state.get("turn_usage", []))
                cost = _compute_turn_cost(usage, model_id, self._model_prices)
                pending = UsageTotals(
                    tokens=usage.total_tokens or 0, cost_usd=cost or 0.0
                )
            reached = await reached_cap(
                ledger, caps, runtime_id, subject.key, pending=pending
            )
            if reached is None:
                return None
            cap, limit, used = reached
            logger.info("runtime.budget_refused", cap=cap, used=used, limit=limit)
            _emit(
                "record_budget_refused",
                definition=self._equipped.definition,
                runtime_id=runtime_id,
                cap=cap,
                limit=limit,
                used=used,
            )
            return caps.message

        return gate

    async def _record_usage(
        self, result: TurnResult, model_id: str | None, tool_calls: int
    ) -> None:
        """Write the turn to the ledger, where it was priced (ADR-011 part 5).

        Only a turn with a subject is recorded. The turn is already paid for,
        so a failed write is logged and never loses the reply.
        """
        subject = current_subject()
        if self._usage_ledger is None or subject is None:
            return
        usage = result.usage
        try:
            await self._usage_ledger.record(
                UsageRecord(
                    runtime_id=self._runtime_id,
                    subject_key=subject.key,
                    actor=_actor_label(),
                    model_id=model_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    total_tokens=usage.total_tokens,
                    cost_usd=usage.cost_usd,
                    model_calls=usage.model_calls,
                    tool_calls=tool_calls,
                    timed_out=result.timed_out,
                )
            )
        except Exception:
            logger.exception("runtime.usage_record_failed")

    def _finish_turn(
        self,
        messages: list[AnyMessage],
        *,
        usage: TurnUsage,
        session_id: str,
        model_id: str | None,
        outcome: str,
        duration_s: float,
        timed_out: bool = False,
        timeout_s: float | None = None,
        budget_refused: bool = False,
    ) -> TurnResult:
        """Attach real cost to *usage*, log one `runtime.turn_usage` event
        (issue #78 Phase 0), record this turn's metrics (issue #78 Phase 0
        Slice 2), and return `messages`/`usage` as a `TurnResult`.

        The log call carries no explicit correlation id: it reuses this
        module's existing `structlog` logger, which already picks up
        whatever `request_id`/`thread_id` contextvars the caller bound
        (`observability/middleware.py`'s `RequestIdMiddleware` for the
        adapter path) — the same mechanism every other `runtime.*` log line
        in this file already relies on.

        Both callers of this method (the "ok" and "timeout" return points in
        `run_turn_with_usage`) are this turn's single choke point for
        `record_turn` -- see that method's own comment for the "error"
        outcome, which never reaches here (it is recorded and re-raised
        before this method could be called).
        """
        cost_usd = _compute_turn_cost(usage, model_id, self._model_prices)
        usage = dataclasses.replace(usage, cost_usd=cost_usd)
        logger.info(
            "runtime.turn_usage",
            session_id=session_id,
            subject=_subject_key(),
            actor=_actor_label(),
            model_id=model_id,
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            total_tokens=usage.total_tokens,
            cost_usd=usage.cost_usd,
        )
        record_turn(
            self._metrics,
            runtime_id=self._runtime_id,
            outcome=outcome,
            duration_s=duration_s,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            actor_kind=_actor_kind(),
        )
        return TurnResult(
            messages=messages,
            usage=usage,
            timed_out=timed_out,
            timeout_s=timeout_s,
            budget_refused=budget_refused,
        )
