"""Regression guard: what every role and deployment resolves to today.

Written BEFORE `extends:` became a real directive, to prove that making the
declaration real moved nothing: every deployment manifest already named the
parent the directory convention picks.

It then did its second job. Introducing `base` and `agent` and re-parenting
the four existing roles under them IS a deliberate behaviour change, and this
file is where it had to be stated rather than discovered:

**Platform roles** — `sales-agent`, `data-agent` and `summary-agent` each gain
`escalation_notifier` and `send:escalation` by inheriting from `agent`. That
is the point of a base: every agent can reach a human, and no descendant has
to restate it. `orchestrator` gains nothing, having already declared both.

**Deployments — unchanged, and that is the load-bearing part.** A deployment
lists its own tools and may only narrow, so ACME's resolved sales-agent still
has exactly its five and data-agent its four. Its `permissions: inherit` does
pick up `send:escalation`, which is inert: the injector resolves tools, never
permissions, and neither deployment declares `escalation_notifier`.

If anything in the DEPLOYMENT section below changes, the taxonomy leaked into
what actually runs, and that is a bug rather than an improvement.

Expected values are literals, captured from the resolver and written out by
hand. They are deliberately NOT derived from the object under test: a test
that compares the resolver to itself cannot fail.
"""
from __future__ import annotations

from typing import Any

import pytest

from agentsys.harness.loader import resolve

# --- Platform roles, resolved with no deployment --------------------------

PINNED_PLATFORM: dict[str, dict[str, Any]] = {
    "orchestrator": {
        # Order is parent-first: `agent` contributes session_state and
        # escalation_notifier, then the role's own additions in declared
        # order. orchestrator already named both, so it gains nothing —
        # they are deduplicated, not repeated.
        "tools": ["session_state", "escalation_notifier", "client_lookup"],
        "permissions": [
            "read:client_registry",
            "read:session",
            "send:escalation",
            "spawn:data-agent",
            "spawn:sales-agent",
            "spawn:summary-agent",
            "write:session",
        ],
        "autonomy": "supervised",
        "context": {"org_context": True, "session": True, "user_identity": True},
    },
    "sales-agent": {
        "tools": [
            "session_state",
            "escalation_notifier",  # gained from `agent`
            "message_sender",
            "catalog_search",
            "order_writer",
            "client_lookup",
        ],
        "permissions": [
            "read:catalog",
            "read:client_registry",
            "read:price_lists",
            "read:session",  # gained from `base`
            "send:escalation",  # gained from `agent`
            "send:message",
            "write:order_items",
            "write:orders",
        ],
        "autonomy": "supervised",
        "context": {"org_context": False, "session": True, "user_identity": True},
    },
    "data-agent": {
        "tools": [
            "session_state",
            "escalation_notifier",  # gained from `agent`
            "catalog_search",
            "client_lookup",
            "knowledge_retrieval",
            "run_report",
        ],
        "permissions": [
            "read:catalog",
            "read:client_registry",
            "read:knowledge_base",
            "read:reports",
            "read:session",
            "send:escalation",  # gained from `agent`
        ],
        # `full` under a `supervised` parent, on purpose. Role-to-role
        # composition lets a child declare its own autonomy; only a
        # DEPLOYMENT is held to its role's ceiling.
        "autonomy": "full",
        "context": {"org_context": True, "session": True, "user_identity": True},
    },
    "summary-agent": {
        "tools": [
            "session_state",
            "escalation_notifier",  # gained from `agent`
            "conversation_summarizer",
            "knowledge_retrieval",
        ],
        "permissions": [
            "read:conversation_logs",
            "read:knowledge_base",
            "read:session",
            "send:escalation",  # gained from `agent`
            "write:summary_output",
        ],
        "autonomy": "full",
        "context": {"org_context": False, "session": True, "user_identity": True},
    },
    "agent": {
        "tools": ["session_state", "escalation_notifier"],
        "permissions": ["read:session", "send:escalation"],
        "autonomy": "supervised",
        "context": {"org_context": False, "session": True, "user_identity": True},
    },
}

# --- Deployment overrides, which may only NARROW their platform role ------

PINNED_DEPLOYMENT: dict[str, dict[str, Any]] = {
    # TOOLS ARE UNCHANGED by the taxonomy, and that is the assertion that
    # matters: a deployment lists its own and may only narrow, so what
    # actually runs is exactly what ran before.
    #
    # PERMISSIONS did move, because both manifests say `permissions: inherit`
    # and the resolved role now carries `read:session` and `send:escalation`.
    # Both are inert here: the injector resolves the tool surface as
    # `role.permissions ∩ granted`, so a permission with no declared tool
    # grants nothing. The ACME data-agent manifest already documents this
    # exact situation for `read:knowledge_base`.
    "sales-agent": {
        "tools": [
            "message_sender",
            "catalog_search",
            "order_writer",
            "session_state",
            "client_lookup",
        ],
        "permissions": [
            "read:catalog",
            "read:client_registry",
            "read:price_lists",
            "read:session",  # inherited, inert
            "send:escalation",  # inherited, inert — no escalation_notifier here
            "send:message",
            "write:order_items",
            "write:orders",
        ],
        "autonomy": "supervised",
        "context": {"org_context": False, "session": True, "user_identity": True},
    },
    "data-agent": {
        "tools": ["run_report", "client_lookup", "catalog_search", "session_state"],
        "permissions": [
            "read:catalog",
            "read:client_registry",
            "read:knowledge_base",
            "read:reports",
            "read:session",
            "send:escalation",  # inherited, inert
        ],
        "autonomy": "full",
        "context": {"org_context": True, "session": True, "user_identity": True},
    },
}


@pytest.mark.parametrize("role_type", sorted(PINNED_PLATFORM))
def test_platform_role_resolves_to_its_pinned_definition(role_type: str) -> None:
    expected = PINNED_PLATFORM[role_type]
    definition = resolve(role_type)

    assert definition.role_name == role_type
    assert definition.deployment is None
    assert list(definition.tools) == expected["tools"]
    assert sorted(definition.permissions) == expected["permissions"]
    assert definition.autonomy == expected["autonomy"]
    assert definition.context == expected["context"]
    # No platform role declares execution_limits today. If one starts to, that
    # is a real change and this line should be updated deliberately.
    assert definition.execution_limits is None


@pytest.mark.parametrize("role_type", sorted(PINNED_DEPLOYMENT))
def test_deployment_resolves_to_its_pinned_definition(role_type: str) -> None:
    expected = PINNED_DEPLOYMENT[role_type]
    definition = resolve(role_type, client="acme")

    assert definition.role_name == role_type
    assert definition.deployment == "acme"
    assert list(definition.tools) == expected["tools"]
    assert sorted(definition.permissions) == expected["permissions"]
    assert definition.autonomy == expected["autonomy"]
    assert definition.context == expected["context"]
    assert definition.execution_limits is None


def test_every_deployment_tool_is_allowed_by_its_platform_role() -> None:
    """The subtractive invariant, stated independently of the pins above.

    This is the property that must survive the inheritance work: whatever
    `extends:` starts to mean for role-to-role composition, a deployment must
    never end up with a tool its resolved platform role does not allow.
    """
    for role_type in sorted(PINNED_DEPLOYMENT):
        platform_tools = set(resolve(role_type).tools)
        deployment_tools = set(resolve(role_type, client="acme").tools)

        assert deployment_tools <= platform_tools, (
            f"{role_type}/acme widened its tool surface: "
            f"{sorted(deployment_tools - platform_tools)}"
        )
