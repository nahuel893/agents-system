"""Built-in defaults for the tools the base roles declare (issue #168).

Every role-defined agent extends ``platform/roles/agent``, whose manifest
declares ``escalation_notifier``. Without a default, the smallest custom
agent could not be built until its author registered an escalation tool,
usually one they have no channel for.

``build_runtime`` fills that one tool from here when the caller's registry
lacks it. The default is honest about what it is:

- It reaches NO human. The model is told so, with the same text the platform
  uses when no escalation channel is configured, so it cannot promise the
  user a callback.
- Every call logs a WARNING (``escalation.not_delivered``) and records an
  ``escalation_not_delivered`` audit event.
- A tool the caller registers under the same name always wins.
- The served app never uses it: ``create_app``'s lifespan builds with
  ``builtin_defaults=False`` (a missing tool still fails boot) and refuses to
  boot when the default is found in the caller's registry.

No predefined role's tools or permissions change: the default carries the
tool's existing contract (``send:escalation``, tier T2), so it is still
denied unless the caller grants ``send:escalation``.
"""

from __future__ import annotations

from typing import Any

import structlog

from agents_system.connectors.platform_connectors import (
    ConnectorOutput,
    build_escalation_notifier_tool_spec,
)
from agents_system.harness.injector import _emit
from agents_system.harness.loader import AgentDefinition
from agents_system.harness.registry import ToolSpec

logger = structlog.get_logger(__name__)

ESCALATION_TOOL = "escalation_notifier"


class BuiltinDefaultToolSpec(ToolSpec):
    """A ``ToolSpec`` the platform supplied because the registry lacked it.

    A marker type, so the served path can recognise and refuse one however
    it got into a registry (``is_builtin_default``).
    """


def is_builtin_default(spec: ToolSpec) -> bool:
    """Whether ``spec`` is one of this module's built-in defaults."""
    return isinstance(spec, BuiltinDefaultToolSpec)


def builtin_default_tools(definition: AgentDefinition) -> dict[str, ToolSpec]:
    """The built-in default for each base-role tool, bound to ``definition``.

    Bound so that the audit event of a call names the agent that made it.
    """
    return {ESCALATION_TOOL: _default_escalation_notifier(definition)}


def _default_escalation_notifier(definition: AgentDefinition) -> ToolSpec:
    # The not-configured spec: same name, description, input schema, tier
    # and permission as the real tool, and a connector that answers "no
    # channel is configured, NO human was notified".
    unbound = build_escalation_notifier_tool_spec(None)

    async def escalation_notifier(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        reason = str(inputs.get("reason") or "").strip()
        details = str(inputs.get("details") or "").strip()
        # `details` is free text about the user, so the log line carries
        # only its length, as the real tool logs only `reason`; the audit
        # event below records it through the audit policy's redactor.
        logger.warning(
            "escalation.not_delivered",
            role=definition.role_name,
            deployment=definition.deployment,
            reason=reason,
            details_chars=len(details),
            message=(
                "The built-in default escalation_notifier handled this "
                "escalation: NO human was notified. Register your own "
                "escalation_notifier ToolSpec to deliver escalations."
            ),
        )
        _emit(
            "record_escalation_not_delivered",
            definition=definition,
            tool_name=ESCALATION_TOOL,
            reason=reason,
            details=details,
        )
        result: ConnectorOutput = await unbound.connector(inputs, session=session)
        return result

    return BuiltinDefaultToolSpec(
        name=unbound.name,
        required_permissions=unbound.required_permissions,
        connector=escalation_notifier,
        tier=unbound.tier,
        description=unbound.description,
        input_schema=unbound.input_schema,
    )
