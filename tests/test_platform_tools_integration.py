"""Integration tests: platform tool stubs through the real harness stack.

Every layer is real — the loader reads ``platform/roles/`` from disk, the
injector narrows the tool surface, the factory assembles the
``EquippedRuntime``, and the interceptor executes the real stub connectors.
All connectors are deterministic stubs with no external dependencies, so this
file is intentionally NOT marked ``@pytest.mark.integration``: pyproject
deselects that marker by default (``addopts = "-m 'not integration'"``) and
these tests must run in the default suite.

The role list is discovered from ``platform/roles/`` on disk, not hardcoded, so
a role added tomorrow is boot-checked without anyone remembering to extend a
tuple. Expected tool surfaces are pinned literals in ``platform_role_contract``
so the boot assertion stops grading a manifest against itself.

asyncio_mode = "auto" (set in pyproject.toml) — async tests need NO marker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from platform_role_contract import (
    EXPECTED_ROLE_TOOLS,
    PINNED_ROLES,
    discover_platform_roles,
)

_ESCALATION_INPUT = {"reason": "customer_angry", "details": "Asked for a manager"}


@dataclass
class SpyEmbedder:
    """Embedder that records calls but never actually embeds."""

    calls: list[list[str]] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=lambda: [[0.1, 0.2, 0.3]])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


def _registry() -> Any:
    """Pure-stub registry — fast, no embedder, all 8 tools."""
    from agentsys.connectors.stubs import build_badie_registry

    return build_badie_registry()


def _rag_registry() -> Any:
    """The registry production actually boots with (main.py, scripts/chat.py)."""
    from agentsys.config import Settings
    from agentsys.connectors.rag_connector import build_badie_rag_registry

    return build_badie_rag_registry(Settings(_env_file=None), embedder=SpyEmbedder())


def _build_runtime(role_type: str, granted_permissions: Any = None) -> Any:
    """Boot a generic role through the real loader + injector + factory.

    Defaults to granting the role's own resolved manifest permissions
    (mirrors main.py: grants are data-driven from the definition).
    """
    from agentsys.harness import loader
    from agentsys.harness.factory import build_runtime

    definition = loader.resolve(role_type, client=None)
    grants = definition.permissions if granted_permissions is None else granted_permissions
    return build_runtime(role_type, _registry(), grants, client=None)


# ---------------------------------------------------------------------------
# Scenario 0 — the role list under test tracks the roles that exist on disk
# ---------------------------------------------------------------------------

def test_platform_roles_on_disk_match_the_pinned_contract() -> None:
    """A new role folder must be pinned before it is considered covered.

    Without this, adding ``platform/roles/billing-agent/`` leaves the suite
    green while ``main.py`` fails to boot the role in its lifespan loop.
    """
    assert set(discover_platform_roles()) == set(EXPECTED_ROLE_TOOLS), (
        "platform/roles/ and EXPECTED_ROLE_TOOLS disagree — pin the new role's "
        "expected tool surface in tests/platform_role_contract.py"
    )


# ---------------------------------------------------------------------------
# Scenario 1 — every role on disk boots end-to-end, against BOTH registries
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role_type", discover_platform_roles())
def test_every_platform_role_boots_end_to_end(role_type: str) -> None:
    from agentsys.harness import loader
    from agentsys.harness.factory import build_runtime
    from agentsys.harness.injector import resolve_tool_surface

    # loader: real platform/roles/{role_type} definition from disk
    definition = loader.resolve(role_type, client=None)
    registry = _registry()

    # injector: the role's own manifest permissions are the grants
    surface = resolve_tool_surface(definition, registry, definition.permissions)
    assert surface.denied == ()

    # factory: the assembled runtime carries every manifest tool, nothing denied
    runtime = build_runtime(role_type, registry, definition.permissions, client=None)
    assert runtime.denied_tools == ()
    assert len(runtime.tools) == len(definition.tools)


@pytest.mark.parametrize("role_type", discover_platform_roles())
def test_every_platform_role_boots_against_the_production_registry(
    role_type: str,
) -> None:
    """Mirror the production wiring in main.py: async RAG catalog + 7 sync stubs."""
    from agentsys.harness import loader
    from agentsys.harness.factory import build_runtime

    definition = loader.resolve(role_type, client=None)

    runtime = build_runtime(
        role_type, _rag_registry(), definition.permissions, client=None
    )

    assert runtime.denied_tools == ()
    assert len(runtime.tools) == len(definition.tools)


@pytest.mark.parametrize("role_type", PINNED_ROLES)
def test_booted_runtime_carries_the_pinned_tool_surface(role_type: str) -> None:
    """Expected tools are a literal, not ``set(definition.tools)``.

    Comparing the runtime surface to the manifest that produced it cannot catch
    a tool being deleted from that manifest — both sides shrink together.
    """
    from agentsys.harness import loader

    definition = loader.resolve(role_type, client=None)
    runtime = _build_runtime(role_type)

    assert set(definition.tools) == EXPECTED_ROLE_TOOLS[role_type]
    assert {t.name for t in runtime.tools} == EXPECTED_ROLE_TOOLS[role_type]


# ---------------------------------------------------------------------------
# Scenario 2 — each new platform tool executes through the real interceptor
# ---------------------------------------------------------------------------

async def test_knowledge_retrieval_executes_on_data_agent() -> None:
    from agentsys.harness.interceptor import intercept

    runtime = _build_runtime("data-agent")

    result = await intercept("knowledge_retrieval", {"q": "pricing"}, runtime)

    assert result.revalidated is False  # read tool — no call-time revalidation
    assert [hit["id"] for hit in result.output["results"]] == ["kb-001"]


async def test_conversation_summarizer_executes_on_summary_agent() -> None:
    from agentsys.harness.interceptor import intercept

    runtime = _build_runtime("summary-agent")

    result = await intercept(
        "conversation_summarizer", {"session_id": "s-001"}, runtime
    )

    assert result.revalidated is False
    assert result.output["session_id"] == "s-001"
    assert result.output["message_count"] == 3
    assert "purchase" in result.output["summary"]


async def test_escalation_notifier_executes_on_orchestrator() -> None:
    from agentsys.harness.interceptor import intercept

    runtime = _build_runtime("orchestrator")

    result = await intercept(
        "escalation_notifier",
        _ESCALATION_INPUT,
        runtime,
        current_permissions=["send:escalation"],  # sensitive — must revalidate
    )

    assert result.revalidated is True
    assert result.output["status"] == "notified"
    assert isinstance(result.output["escalation_id"], str)


# ---------------------------------------------------------------------------
# Scenario 3 — Layer-2 sensitivity proof for escalation_notifier (send: prefix)
# ---------------------------------------------------------------------------

async def test_escalation_notifier_blocked_without_current_permissions() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    runtime = _build_runtime("orchestrator")

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept("escalation_notifier", _ESCALATION_INPUT, runtime)

    assert exc_info.value.tool_name == "escalation_notifier"
    assert exc_info.value.reason == "revalidation_required"


async def test_escalation_notifier_revalidated_with_send_escalation() -> None:
    from agentsys.harness.interceptor import intercept

    runtime = _build_runtime("orchestrator")

    result = await intercept(
        "escalation_notifier",
        _ESCALATION_INPUT,
        runtime,
        current_permissions=["send:escalation"],
    )

    assert result.revalidated is True
    assert result.output["status"] == "notified"


async def test_escalation_notifier_blocked_when_permission_revoked() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    runtime = _build_runtime("orchestrator")

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(
            "escalation_notifier",
            _ESCALATION_INPUT,
            runtime,
            current_permissions=["read:client_registry"],  # send:escalation revoked
        )

    assert exc_info.value.tool_name == "escalation_notifier"
    assert exc_info.value.reason == "permission_revoked"


# ---------------------------------------------------------------------------
# Scenario 4 — negative surface: registry-known tool absent from the role surface
# ---------------------------------------------------------------------------

async def test_order_writer_blocked_on_data_agent_runtime() -> None:
    from agentsys.harness.interceptor import PolicyViolation, intercept

    # order_writer EXISTS in the registry — the surface, not the registry, is
    # the authority at call time.
    assert "order_writer" in _registry().names()
    runtime = _build_runtime("data-agent")

    with pytest.raises(PolicyViolation) as exc_info:
        await intercept(
            "order_writer",
            {"client_id": "cl-001", "items": []},
            runtime,
            current_permissions=["write:orders", "write:order_items"],
        )

    assert exc_info.value.tool_name == "order_writer"
    assert exc_info.value.reason == "not_in_surface"


# ---------------------------------------------------------------------------
# Scenario 5 — Layer-1 RBAC narrowing: boot succeeds with a partial surface
# ---------------------------------------------------------------------------

def test_data_agent_boots_with_knowledge_retrieval_denied() -> None:
    from agentsys.harness import loader

    definition = loader.resolve("data-agent", client=None)
    narrowed = [p for p in definition.permissions if p != "read:knowledge_base"]

    runtime = _build_runtime("data-agent", narrowed)

    assert {t.name for t in runtime.tools} == {
        "catalog_search",
        "client_lookup",
        "session_state",
    }
    denied = dict(runtime.denied_tools)
    assert set(denied) == {"knowledge_retrieval"}
    assert "read:knowledge_base" in denied["knowledge_retrieval"]
