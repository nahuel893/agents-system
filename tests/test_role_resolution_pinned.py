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
lists its own tools and may only narrow, so the generic client's resolved
sales-agent still has exactly its five and data-agent its four. Its `permissions: inherit` does
pick up `send:escalation`, which is inert: the injector resolves tools, never
permissions, and neither deployment declares `escalation_notifier`.

If anything in the DEPLOYMENT section below changes, the taxonomy leaked into
what actually runs, and that is a bug rather than an improvement.

Expected values are literals, captured from the resolver and written out by
hand. They are deliberately NOT derived from the object under test: a test
that compares the resolver to itself cannot fail.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from agentsys.harness.loader import RootConfig, resolve

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_CLIENT_A_DEPLOYMENTS = (
    _REPO_ROOT / "tests" / "fixtures" / "agents" / "overrides" / "deployments"
)


def _client_a_roots() -> RootConfig:
    return RootConfig(
        platform_root=_REPO_ROOT / "platform",
        deployments_root=_CLIENT_A_DEPLOYMENTS,
    )


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
    "operator-agent": {
        "tools": ["session_state", "escalation_notifier", "use_term", "read_file"],
        "permissions": [
            "exec:command",
            "read:files",
            "read:session",
            "send:escalation",
        ],
        "autonomy": "supervised",
        "context": {"org_context": False, "session": True, "user_identity": True},
    },
    "support-agent": {
        "tools": [
            "session_state",
            "escalation_notifier",
            "knowledge_retrieval",
            "conversation_summarizer",
            "client_lookup",
            "message_sender",
        ],
        "permissions": [
            "read:client_registry",
            "read:conversation_logs",
            "read:knowledge_base",
            "read:session",
            "send:escalation",
            "send:message",
        ],
        "autonomy": "supervised",
        "context": {"org_context": True, "session": True, "user_identity": True},
    },
    "developer-agent": {
        "tools": [
            "session_state",
            "escalation_notifier",
            "use_term",
            "read_file",
            "knowledge_retrieval",
        ],
        "permissions": [
            "exec:command",
            "read:files",
            "read:knowledge_base",
            "read:session",
            "send:escalation",
        ],
        "autonomy": "supervised",
        "context": {"org_context": True, "session": True, "user_identity": True},
    },
    "accountant-agent": {
        "tools": [
            "session_state",
            "escalation_notifier",
            "run_report",
            "knowledge_retrieval",
        ],
        "permissions": [
            "read:knowledge_base",
            "read:reports",
            "read:session",
            "send:escalation",
        ],
        "autonomy": "supervised",
        "context": {"org_context": True, "session": True, "user_identity": True},
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
    # grants nothing. The generic data-agent fixture pins this inert
    # `read:knowledge_base` permission.
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
    # Only the operator branch tightens the platform ceiling, and it does so
    # because every call it makes is a subprocess. A role gaining or losing
    # limits is a safety change, so it is pinned per-role rather than assumed.
    expected_limits = (
        {
            "tool_call_timeout_s": 10,
            "total_execution_timeout_s": 30,
            "max_tool_calls": 10,
        }
        if role_type in {"operator-agent", "developer-agent"}
        else None
    )
    assert definition.execution_limits == expected_limits


@pytest.mark.parametrize("role_type", sorted(PINNED_DEPLOYMENT))
def test_deployment_resolves_to_its_pinned_definition(role_type: str) -> None:
    expected = PINNED_DEPLOYMENT[role_type]
    definition = resolve(role_type, client="client-a", roots=_client_a_roots())

    assert definition.role_name == role_type
    assert definition.deployment == "client-a"
    assert list(definition.tools) == expected["tools"]
    assert sorted(definition.permissions) == expected["permissions"]
    assert definition.autonomy == expected["autonomy"]
    assert definition.context == expected["context"]
    assert definition.execution_limits is None


def test_every_deployment_tool_is_allowed_by_its_platform_role() -> None:
    """Every shipped deployment stays inside its role's surface."""
    for role_type in sorted(PINNED_DEPLOYMENT):
        platform_tools = set(resolve(role_type).tools)
        deployment_tools = set(
            resolve(role_type, client="client-a", roots=_client_a_roots()).tools
        )

        assert deployment_tools <= platform_tools, (
            f"{role_type}/client-a widened its tool surface: "
            f"{sorted(deployment_tools - platform_tools)}"
        )


def test_a_deployment_that_tries_to_widen_is_refused(tmp_path: pathlib.Path) -> None:
    """The ENFORCEMENT, which the test above cannot reach.

    That one grades the manifests currently on disk — all of which are
    correct — so deleting the subtractive validator leaves it green. It
    proves the shipped deployments are well-formed, not that a malformed one
    would be caught. This constructs the malformed one.
    """
    import pytest

    from agentsys.harness.loader import DefinitionError, RootConfig

    dep = tmp_path / "greedy" / "summary-agent"
    dep.mkdir(parents=True)
    (dep / "role.md").write_text("---\nname: summary-agent\n---\n\nbody\n")
    (dep / "manifest.md").write_text(
        "---\nrole: summary-agent\ndeployment: greedy\n"
        # `use_term` belongs to the operator branch; summary-agent has no path
        # to it, so asking for it is a deployment trying to widen.
        "tools: [session_state, use_term]\n"
        "skills: []\ncontext: {}\npermissions: inherit\n---\n\nm\n"
    )
    (dep / "policy.md").write_text(
        "---\nrole: summary-agent\nautonomy: supervised\n"
        "execution_limits: null\n---\n\np\n"
    )

    with pytest.raises(DefinitionError) as excinfo:
        resolve(
            "summary-agent",
            client="greedy",
            roots=RootConfig(deployments_root=tmp_path),
        )

    assert "use_term" in str(excinfo.value)


def test_operator_agent_tightens_the_platform_execution_limits() -> None:
    """The role that can spawn processes gets half the default budget.

    Platform defaults allow 60s and 20 tool calls. Every call this role makes
    is a subprocess, and an agent looping on a failing command is a fork bomb
    with good intentions — so the ceiling is a safety decision, not a
    performance one, and it needs an assertion rather than a comment.

    Written as literals: reading them off the resolved definition would grade
    the manifest against itself.
    """
    definition = resolve("operator-agent")

    assert definition.execution_limits == {
        "tool_call_timeout_s": 10,
        "total_execution_timeout_s": 30,
        "max_tool_calls": 10,
    }
    # And it does not take `full` autonomy, unlike two of its cousins: it is
    # the only role in the tree that can change the host.
    assert definition.autonomy == "supervised"


def test_no_role_outside_the_operator_branch_can_reach_the_host() -> None:
    """The whole reason `operator-agent` is a sibling and not a base.

    Role-to-role inheritance is additive with no removal directive, so a host
    permission placed anywhere above the conversational roles could never be
    taken back. Grepping the resolved surface — not the manifests — catches a
    grant that arrives by inheritance rather than declaration.
    """
    from agentsys.harness.loader import RootConfig, _extends_target, _load_role_files
    from platform_role_contract import discover_concrete_platform_roles

    host_permissions = {"exec:command", "read:files"}
    host_tools = {"use_term", "read_file"}
    roots = RootConfig()

    def descends_from_operator(role: str) -> bool:
        """Walk `extends` upward. Computed, never a hardcoded allow-list.

        A hardcoded list would have to be edited alongside every new role,
        and the edit that adds a role to it is exactly the one nobody
        questions. Deriving it from the chain means a role reaches the host
        only by actually descending from `operator-agent`.
        """
        seen: set[str] = set()
        current: str | None = role
        while current and current not in seen:
            if current == "operator-agent":
                return True
            seen.add(current)
            _, parent, _ = _load_role_files(current, roots)
            current = _extends_target(parent) if parent else None
        return False

    for role in discover_concrete_platform_roles():
        definition = resolve(role)
        reaches_host = bool(host_permissions & set(definition.permissions)) or bool(
            host_tools & set(definition.tools)
        )
        if descends_from_operator(role):
            assert reaches_host, (
                f"'{role}' descends from operator-agent but reaches nothing; "
                f"the branch exists to carry those tools"
            )
        else:
            assert not reaches_host, (
                f"'{role}' can reach the host without descending from "
                f"operator-agent. A host capability must be a decision, not "
                f"something inherited from a shared base."
            )


#: Roles that may hold a mutating permission, and the one they may hold.
#:
#: Everything else in the tree is read-only by design, and several manifests
#: SAY so in prose. Prose is not enforcement: `accountant-agent` could gain
#: `write:orders` and the whole suite stayed green until this pin existed.
#:
#: Adding a role here, or widening one, is the edit a reviewer must actually
#: look at. That is the entire purpose of stating it as a literal.
MUTATING_GRANTS: dict[str, frozenset[str]] = {
    "sales-agent": frozenset(
        {"write:orders", "write:order_items", "send:message", "send:escalation"}
    ),
    "orchestrator": frozenset(
        {
            "write:session",
            "send:escalation",
            "spawn:sales-agent",
            "spawn:data-agent",
            "spawn:summary-agent",
        }
    ),
    "summary-agent": frozenset({"write:summary_output", "send:escalation"}),
    # Talks to customers directly, so it may reply — and nothing else.
    "support-agent": frozenset({"send:message", "send:escalation"}),
    # Every agent may reach a human. That is the only grant `agent` adds.
    "agent": frozenset({"send:escalation"}),
    "data-agent": frozenset({"send:escalation"}),
    "operator-agent": frozenset({"send:escalation"}),
    "developer-agent": frozenset({"send:escalation"}),
    # Reads reports and nothing else. NO write of any kind: a wrong entry is
    # found by an auditor rather than by a test.
    "accountant-agent": frozenset({"send:escalation"}),
}


def test_no_role_holds_an_unpinned_mutating_permission() -> None:
    """A `write:`, `send:` or `spawn:` grant must be a reviewed decision.

    Permissions arrive by inheritance as well as declaration, so this reads
    the RESOLVED surface: a mutating grant added to a shared base would show
    up on every descendant here, which is exactly the blast radius worth
    seeing before it ships.
    """
    from platform_role_contract import discover_concrete_platform_roles

    prefixes = ("write:", "send:", "spawn:")

    for role in discover_concrete_platform_roles():
        actual = {p for p in resolve(role).permissions if p.startswith(prefixes)}
        expected = MUTATING_GRANTS.get(role)

        assert expected is not None, (
            f"'{role}' is not pinned in MUTATING_GRANTS. A new role must "
            f"state which mutating permissions it holds, even if that is none."
        )
        assert actual == expected, (
            f"'{role}' mutating grants changed: "
            f"added {sorted(actual - expected)}, "
            f"removed {sorted(expected - actual)}"
        )
