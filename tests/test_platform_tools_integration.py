"""Integration tests: platform tool stubs through the real harness stack.

Every layer is real — the loader reads ``platform/roles/`` from disk, the
injector narrows the tool surface, the factory assembles the
``EquippedRuntime``, and the interceptor executes the real stub connectors.
All connectors are deterministic stubs with no external dependencies, so this
file is intentionally NOT marked ``@pytest.mark.integration``: pyproject
deselects that marker by default (``addopts = "-m 'not integration'"``) and
these tests must run in the default suite.

asyncio_mode = "auto" (set in pyproject.toml) — async tests need NO marker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

GENERIC_ROLES = ("sales-agent", "data-agent", "summary-agent", "orchestrator")

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
# Scenario 1 — every generic role boots end-to-end with its full tool surface
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role_type", GENERIC_ROLES)
def test_generic_role_boots_end_to_end(role_type: str) -> None:
    from agentsys.harness import loader
    from agentsys.harness.factory import build_runtime
    from agentsys.harness.injector import resolve_tool_surface

    # loader: real platform/roles/{role_type} definition from disk
    definition = loader.resolve(role_type, client=None)
    registry = _registry()

    # injector: the role's own manifest permissions are the grants
    surface = resolve_tool_surface(definition, registry, definition.permissions)
    assert surface.denied == ()
    assert {t.name for t in surface.granted} == set(definition.tools)

    # factory: the assembled runtime carries every manifest tool, nothing denied
    runtime = build_runtime(role_type, registry, definition.permissions, client=None)
    assert runtime.denied_tools == ()
    assert {t.name for t in runtime.tools} == set(definition.tools)


def test_orchestrator_boots_against_rag_registry() -> None:
    """Mirror the production wiring in main.py: async RAG catalog + 7 sync stubs."""
    from agentsys.config import Settings
    from agentsys.connectors.rag_connector import build_badie_rag_registry
    from agentsys.harness import loader
    from agentsys.harness.factory import build_runtime

    registry = build_badie_rag_registry(Settings(_env_file=None), embedder=SpyEmbedder())
    definition = loader.resolve("orchestrator", client=None)

    runtime = build_runtime("orchestrator", registry, definition.permissions, client=None)

    assert runtime.denied_tools == ()
    assert {t.name for t in runtime.tools} == set(definition.tools)


# ---------------------------------------------------------------------------
# Scenario 2 — each new platform tool executes through the real interceptor
# ---------------------------------------------------------------------------

async def test_knowledge_retrieval_executes_on_data_agent() -> None:
    from agentsys.harness.interceptor import intercept

    runtime = _build_runtime("data-agent")

    result = await intercept("knowledge_retrieval", {"q": "pricing"}, runtime)

    assert result.revalidated is False  # read tool — no call-time revalidation
    assert isinstance(result.output["results"], list)
    assert result.output["results"]


async def test_conversation_summarizer_executes_on_summary_agent() -> None:
    from agentsys.harness.interceptor import intercept

    runtime = _build_runtime("summary-agent")

    result = await intercept(
        "conversation_summarizer", {"session_id": "s-001"}, runtime
    )

    assert result.revalidated is False
    assert isinstance(result.output["summary"], str)
    assert result.output["summary"]
    assert isinstance(result.output["message_count"], int)


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
