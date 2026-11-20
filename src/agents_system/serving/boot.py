"""Boot checks and the boot plan."""

from __future__ import annotations

from collections.abc import (
    Iterable,
    Mapping,
)
from typing import Any

import structlog

from agents_system.extras import raise_missing_extra

try:
    from fastapi import FastAPI
except ImportError as exc:
    raise_missing_extra("server", "The served app", exc)

from agents_system.agent.spec import Agent
from agents_system.config import Settings
from agents_system.execution_limits import (
    parse_execution_limits,
    parse_timeout_replies,
    parse_tool_error_replies,
)
from agents_system.harness.loader import (
    DefinitionError,
)
from agents_system.services.outbox import (
    DEFAULT_LEASE_DURATION,
)
from agents_system.services.retry_policy import build_retry_policies
from agents_system.serving.channels import _registered_channels
from agents_system.serving.registrations import (
    _BootPlan,
    _bounded_repr,
    _build_registrations,
    _settings_agents,
)


def _warn_if_turn_budget_below_request_timeout(
    runtime_id: str,
    definition: Any,
    settings: Settings,
    deployment_limits: Mapping[str, Any] | None = None,
) -> None:
    """Warn at boot when a runtime's turn budget cannot hold one provider call.

    ``total_execution_timeout_s`` bounds the whole turn and
    ``OPENAI_COMPATIBLE_REQUEST_TIMEOUT_S`` bounds one provider HTTP call, so
    a budget below the request timeout lets a single slow response consume
    the entire turn (issue #172). A turn that uses a tool makes at least two
    such calls, so the useful floor is higher still; this only flags the
    case that is broken outright and stays a warning, never a boot failure:
    a deliberately tight budget is the operator's call to make.

    Only the ``openai_compatible`` provider is bounded by that setting, so
    the check reads the provider the runtime will actually use: its own
    ``model:`` selection when it has one, else ``ADAPTER_PROVIDER``.
    """
    from agents_system.agent.limits import _effective_limits

    selection = getattr(definition, "model", None)
    provider = (
        selection.get("provider")
        if isinstance(selection, Mapping) and selection
        else settings.adapter_provider
    )
    if provider != "openai_compatible":
        return
    total_timeout_s = _effective_limits(
        getattr(definition, "execution_limits", None), deployment_limits
    )["total_execution_timeout_s"]
    request_timeout_s = settings.openai_compatible_request_timeout_s
    if total_timeout_s < request_timeout_s:
        structlog.get_logger().warning(
            "runtime.turn_budget_below_request_timeout",
            runtime_id=runtime_id,
            total_execution_timeout_s=total_timeout_s,
            openai_compatible_request_timeout_s=request_timeout_s,
        )


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
        # issue #128 item 5 -- create_app's own docstring says grants is
        # "keyed by the same ids as agents", exactly like clients above; a
        # grants= with no agents= to key it against was silently accepted
        # instead of refused the same way.
        if grants is not None:
            raise DefinitionError(
                "create_app got grants but no agents: grants is keyed by "
                "the same runtime ids as agents, so it needs agents to "
                "register one. Refusing to boot."
            )
        agents, clients = _settings_agents(
            settings.agent_registrations, settings.agents_root
        )
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

    # issue #128 item 4 -- create_app(grants=...) keys were never
    # cross-checked against what agents actually registers, unlike clients
    # just above: an id create_app(grants=...) names that agents does not
    # register was silently ignored instead of failing boot. Scoped to the
    # caller-supplied grants= only -- never settings.deploy_grants below,
    # which the per-runtime "has no grant entry" check in the lifespan loop
    # already covers, for a distinct, separately-tested scenario (e.g. a
    # DEPLOY_GRANTS entry still keyed by the old, removed scheme -- see
    # test_registration_fails_boot_without_a_grant_for_the_id).
    if grants is not None:
        unknown_grants = [
            _bounded_repr(key) for key in grants if key not in registrations
        ]
        if unknown_grants:
            raise DefinitionError(
                f"grants names runtime id(s) {', '.join(unknown_grants)} "
                f"that {source} does not register. Refusing to boot."
            )

    # A channel id is looked up in the registration as an opaque key, never
    # parsed. One that matches nothing fails boot: an unmatched
    # WHATSAPP_RUNTIME_ID would accept messages no runtime answers, and an
    # unmatched ADAPTER_RUNTIMES id would leave /v1/models listing less than
    # the operator named.
    whatsapp_ids = (
        [settings.whatsapp_runtime_id] if settings.whatsapp_runtime_id else []
    )
    # #22 -- a registered channel's runtime id is checked the same way.
    channel_ids = [
        (f"channels[{name!r}].runtime_id", [channel.runtime_id])
        for name, channel in _registered_channels(app).items()
    ]
    for channel, ids in (
        ("WHATSAPP_RUNTIME_ID", whatsapp_ids),
        ("ADAPTER_RUNTIMES", settings.adapter_runtimes),
        *channel_ids,
    ):
        # issue #128 item 2 -- an id that fails this membership check is, by
        # definition, not yet a validated runtime id and may be unbounded.
        unmatched = [
            _bounded_repr(runtime_id)
            for runtime_id in ids
            if runtime_id not in registrations
        ]
        if unmatched:
            raise DefinitionError(
                f"{channel} names runtime id(s) {', '.join(unmatched)} that "
                f"{source} does not register (registered: "
                f"{sorted(registrations)}).{hint} Refusing to boot."
            )

    # #278 -- set by the deployment only, checked here so a turn that cannot
    # fit the outbox lease, or a limit for a runtime this boot does not
    # build, fails before any resource exists.
    raw_limits = getattr(app.state, "execution_limits", None)
    raw_replies = getattr(app.state, "timeout_reply", None)
    execution_limits = parse_execution_limits(
        raw_limits if raw_limits is not None else settings.execution_limits,
        lease_s=DEFAULT_LEASE_DURATION.total_seconds(),
    )
    timeout_replies = parse_timeout_replies(
        raw_replies if raw_replies is not None else settings.turn_timeout_reply
    )
    raw_error_replies = getattr(app.state, "tool_error_reply", None)
    tool_error_replies = parse_tool_error_replies(
        raw_error_replies
        if raw_error_replies is not None
        else settings.tool_error_reply
    )
    for source, configured in (
        ("execution limits (EXECUTION_LIMITS)", execution_limits),
        ("timeout replies (TURN_TIMEOUT_REPLY)", timeout_replies),
        ("tool error replies (TOOL_ERROR_REPLY)", tool_error_replies),
    ):
        unknown = sorted(set(configured) - set(registrations))
        if unknown:
            raise ValueError(
                f"{source} name runtime id(s) this boot does not build: "
                f"{', '.join(unknown)}. Refusing to boot."
            )

    raw_retry = getattr(app.state, "outbox_retry", None)
    retry_policies = build_retry_policies(
        policies=raw_retry,
        max_attempts=settings.outbox_max_attempts,
        fallback_replies=settings.outbox_fallback_reply,
    )
    unknown_retry = sorted(set(retry_policies) - set(registrations))
    if unknown_retry:
        raise ValueError(
            "outbox retry policies (create_app(outbox_retry=...), "
            "OUTBOX_MAX_ATTEMPTS, OUTBOX_FALLBACK_REPLY) name runtime id(s) "
            f"this boot does not build: {', '.join(unknown_retry)}. "
            "Refusing to boot."
        )

    return _BootPlan(
        registrations=registrations,
        grants=grants if grants is not None else settings.deploy_grants,
        grant_source="create_app(grants=...)"
        if grants is not None
        else "DEPLOY_GRANTS",
        execution_limits=execution_limits,
        timeout_replies=timeout_replies,
        tool_error_replies=tool_error_replies,
        retry_policies=retry_policies,
    )
