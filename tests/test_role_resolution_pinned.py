"""Regression guard: what every role and deployment resolves to today.

Written BEFORE `extends:` became a real directive. Every deployment manifest
in this repository already declares `extends: platform/roles/{its own
role_type}` — the same parent the directory convention picks — so making the
declaration real must change nothing at all.

That is the whole point of this file. If a value below changes, the
inheritance work introduced a behaviour change it did not intend, and that is
a bug in the change rather than an improvement in the platform.

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
        "tools": ["client_lookup", "session_state", "escalation_notifier"],
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
            "send:message",
            "write:order_items",
            "write:orders",
        ],
        "autonomy": "supervised",
        "context": {"org_context": False, "session": True, "user_identity": True},
    },
    "data-agent": {
        "tools": [
            "catalog_search",
            "client_lookup",
            "knowledge_retrieval",
            "run_report",
            "session_state",
        ],
        "permissions": [
            "read:catalog",
            "read:client_registry",
            "read:knowledge_base",
            "read:reports",
            "read:session",
        ],
        "autonomy": "full",
        "context": {"org_context": True, "session": True, "user_identity": True},
    },
    "summary-agent": {
        "tools": ["conversation_summarizer", "knowledge_retrieval", "session_state"],
        "permissions": [
            "read:conversation_logs",
            "read:knowledge_base",
            "read:session",
            "write:summary_output",
        ],
        "autonomy": "full",
        "context": {"org_context": False, "session": True, "user_identity": True},
    },
}

# --- Deployment overrides, which may only NARROW their platform role ------

PINNED_DEPLOYMENT: dict[str, dict[str, Any]] = {
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
