"""The two shipped registries, and the role surfaces they have to satisfy.

This file was `test_connectors_platform_stubs.py` until issue #39 deleted the
module it was named for. What remains is the part that was never about the
stubs: that both registry builders hold every tool the role manifests name,
wire them identically, and that each platform role resolves to a pinned tool
surface. A manifest naming a tool no registry holds makes the role unbootable
through `InjectionError`, so this is the guard on that.

Per-connector behaviour lives with its connector — see
`test_platform_connectors.py` for the three platform-generic tools and
`test_order_writer_connector.py` for `order_writer`.

Assertion policy in this file: expected values are written out as literals, not
derived from the object under test. A test that compares a pure function to
itself ("call it twice, assert equal") cannot fail for any implementation and
is not written here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from platform_role_contract import (
    EXPECTED_ROLE_TOOLS,
    PINNED_ROLES,
    discover_concrete_platform_roles,
)

# run_report arrived with D-023. It is registered unbound when no BI
# engine is configured, because platform/roles/data-agent names it and a
# tool a manifest names but the registry lacks makes the whole role
# unbuildable through InjectionError.
ALL_PLATFORM_TOOLS = {
    # Registered inert by both shipped registries so `operator-agent` can
    # boot. Inert means `use_term` refuses every command until a deployment
    # supplies a TerminalPolicy — the tool being PRESENT is what the injector
    # needs, and being USABLE is a separate, explicit decision.
    "use_term",
    "read_file",
    "catalog_search",
    "client_lookup",
    "order_writer",
    "message_sender",
    "session_state",
    "knowledge_retrieval",
    "conversation_summarizer",
    "escalation_notifier",
    "run_report",
}



#: Tools both registry builders must wire identically. Two exclusions, both
#: because the connector is a closure over something the builder is given
#: rather than a shared module-level function, so object identity cannot hold:
#:
#:   catalog_search — the RAG registry swaps in a semantic-search connector
#:                    closed over the embedder, with its own schema.
#:   run_report     — closed over the BI engine, which is exactly the point:
#:                    the RAG registry can be handed a real read-only engine
#:                    while the stub registry stays unbound.
#:   order_writer   — closed over the deployment's OrderWriter, for the same
#:                    reason (issue #39): the platform has no order system of
#:                    its own, so the stub registry stays unbound while a
#:                    consuming application binds a real one.
#:
#:   knowledge_retrieval / conversation_summarizer / escalation_notifier —
#:                    joined that list when they stopped being module-level
#:                    stub functions (issue #39). Each is now closed over a
#:                    port the platform does not implement, so the two
#:                    registries can bind them differently on purpose.
#:
#: Drift is guarded structurally instead: both registries build these through
#: one factory each, so there are no hand-duplicated specs to diverge — which
#: is the failure mode this test exists to catch for the others. The contract
#: the model sees is compared in `FACTORY_BUILT_TOOLS` below.
FACTORY_BUILT_TOOLS = (
    "order_writer",
    "knowledge_retrieval",
    "conversation_summarizer",
    "escalation_notifier",
)

SHARED_TOOLS = tuple(
    sorted(
        ALL_PLATFORM_TOOLS
        - {"catalog_search", "run_report"}
        - set(FACTORY_BUILT_TOOLS)
    )
)


@dataclass
class SpyEmbedder:
    """Embedder that records calls but never actually embeds."""

    calls: list[list[str]] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=lambda: [[0.1, 0.2, 0.3]])

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return self.vectors


def _settings() -> Any:
    from agentsys.config import Settings

    # `_env_file=None` builds Settings without reading the developer's .env, so
    # this suite does not depend on local machine state. It is a real parameter
    # of `BaseSettings.__init__`, but pydantic synthesizes a model `__init__`
    # from the FIELDS, which shadows the inherited signature — so every type
    # checker reports it as unknown. Runtime is correct; the annotation is the
    # thing that is wrong.
    return Settings(_env_file=None)  # type: ignore[call-arg]


def _stub_registry() -> Any:
    from agentsys.connectors.stubs import build_acme_registry

    return build_acme_registry()


def _rag_registry() -> Any:
    from agentsys.connectors.rag_connector import build_acme_rag_registry

    return build_acme_rag_registry(_settings(), embedder=SpyEmbedder())


#: Both registry builders, so every registry assertion also runs against the
#: one production actually uses (build_acme_rag_registry — main.py,
#: scripts/chat.py, scripts/smoke_chat.py), not only the stub registry.
REGISTRY_BUILDERS = {"stub": _stub_registry, "rag": _rag_registry}




def _without_descriptions(node: Any) -> Any:
    """Drop every ``description`` key so schema parity ignores prose drift."""
    if isinstance(node, dict):
        return {
            key: _without_descriptions(value)
            for key, value in node.items()
            if key != "description"
        }
    if isinstance(node, list):
        return [_without_descriptions(value) for value in node]
    return node


# ---------------------------------------------------------------------------
# Registry builders — both must contain all 8 tools, wired identically
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("builder_name", sorted(REGISTRY_BUILDERS))
def test_registry_contains_all_platform_tools(builder_name: str) -> None:
    registry = REGISTRY_BUILDERS[builder_name]()

    assert set(registry.names()) == ALL_PLATFORM_TOOLS
    assert registry.get("knowledge_retrieval").required_permissions == (
        "read:knowledge_base",
    )
    assert registry.get("conversation_summarizer").required_permissions == (
        "read:conversation_logs",
    )
    assert registry.get("escalation_notifier").required_permissions == (
        "send:escalation",
    )


@pytest.mark.parametrize("builder_name", sorted(REGISTRY_BUILDERS))
def test_platform_tool_input_schemas_have_required_lists(builder_name: str) -> None:
    registry = REGISTRY_BUILDERS[builder_name]()

    assert registry.get("knowledge_retrieval").input_schema["required"] == ["q"]
    assert registry.get("conversation_summarizer").input_schema["required"] == [
        "session_id"
    ]
    assert set(registry.get("escalation_notifier").input_schema["required"]) == {
        "reason",
        "details",
    }


@pytest.mark.parametrize("tool_name", SHARED_TOOLS)
def test_both_registries_wire_shared_tools_identically(tool_name: str) -> None:
    """The two builders hand-duplicate their specs, so they can drift.

    Production uses build_acme_rag_registry (main.py, scripts/chat.py,
    scripts/smoke_chat.py); most tests reach for build_acme_registry. Drop
    ``session_id`` from the RAG copy of conversation_summarizer's ``required``
    and every production summary silently becomes ``session_id="s-unknown"``.
    Schema prose is compared with descriptions stripped: they already differ by
    a trailing period on knowledge_retrieval's ``q``.
    """
    stub_spec = _stub_registry().get(tool_name)
    rag_spec = _rag_registry().get(tool_name)

    assert stub_spec.required_permissions == rag_spec.required_permissions
    assert stub_spec.connector is rag_spec.connector
    assert _without_descriptions(stub_spec.input_schema) == _without_descriptions(
        rag_spec.input_schema
    )


# ---------------------------------------------------------------------------
# Injector-level — every platform role resolves against a literal expectation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role_type", PINNED_ROLES)
def test_platform_role_resolves_its_pinned_tool_surface(role_type: str) -> None:
    """Expected surface is a literal, NOT ``set(definition.tools)``.

    Grading the resolved surface against the manifest that produced it means
    deleting a tool from a manifest keeps the assertion green (2 == 2).
    """
    from agentsys.harness import loader
    from agentsys.harness.injector import resolve_tool_surface

    definition = loader.resolve(role_type, client=None)
    registry = _stub_registry()

    # Mirrors main.py: the role's own resolved permissions are the grants.
    result = resolve_tool_surface(
        definition, registry, granted_permissions=definition.permissions
    )

    assert result.denied == ()
    assert set(definition.tools) == EXPECTED_ROLE_TOOLS[role_type]
    assert {t.name for t in result.granted} == EXPECTED_ROLE_TOOLS[role_type]


@pytest.mark.parametrize("role_type", discover_concrete_platform_roles())
@pytest.mark.parametrize("builder_name", sorted(REGISTRY_BUILDERS))
def test_every_tool_declared_on_disk_exists_in_both_registries(
    builder_name: str, role_type: str
) -> None:
    """The role list comes from disk, so role #5 is covered without a tuple edit.

    A manifest naming a tool no registry holds makes the injector raise
    ``InjectionError`` and the role unbootable — the failure class this branch
    exists to eliminate.
    """
    from agentsys.harness import loader

    definition = loader.resolve(role_type, client=None)
    registry_names = set(REGISTRY_BUILDERS[builder_name]().names())

    missing = sorted(set(definition.tools) - registry_names)
    assert missing == [], (
        f"role '{role_type}' declares {missing}, absent from the "
        f"{builder_name} registry — the role cannot boot"
    )


# --- No registered connector may fabricate a result (issue #39) -------------


#: tool -> (inputs, the error_kind an unbound registry must answer with).
#: Every one of these used to return a confident success from a hardcoded
#: fixture: three canned knowledge hits, one fixed summary sentence, and
#: `esc-NNNN` for an escalation no human ever received.
_UNBOUND_EXPECTATIONS = {
    "knowledge_retrieval": ({"q": "return policy"}, "knowledge_not_configured"),
    "conversation_summarizer": (
        {"session_id": "s-1"},
        "summarization_not_configured",
    ),
    "escalation_notifier": (
        {"reason": "customer_angry", "details": "third failed delivery"},
        "escalation_not_configured",
    ),
}

#: Keys that mean the tool answered. Absence of these is the real assertion:
#: the model reads the whole dict, so an error alongside a success shape is
#: still read as success.
_FABRICATED_KEYS = ("results", "summary", "message_count", "escalation_id")


@pytest.mark.parametrize("builder_name", sorted(REGISTRY_BUILDERS))
@pytest.mark.parametrize("tool_name", sorted(_UNBOUND_EXPECTATIONS))
async def test_platform_tools_in_every_registry_refuse_instead_of_fabricating(
    builder_name: str, tool_name: str
) -> None:
    """Neither shipped registry may answer one of these on its own authority.

    `escalation_notifier` is the one that matters most: it is a `send:` tool
    whose whole purpose is to put a human in the loop, every role in the
    taxonomy inherits it, and it used to report "notified" with no channel
    behind it — so the single path a stuck customer had was the one that lied
    about working.
    """
    import inspect

    inputs, expected_kind = _UNBOUND_EXPECTATIONS[tool_name]
    spec = REGISTRY_BUILDERS[builder_name]().get(tool_name)

    result = spec.connector(inputs)
    if inspect.isawaitable(result):
        result = await result

    assert result["error_kind"] == expected_kind
    assert result.get("status") != "notified"
    fabricated = [key for key in _FABRICATED_KEYS if key in result]
    assert fabricated == [], (
        f"the {builder_name} registry's {tool_name} returned {fabricated} "
        f"without a system behind it: {result!r}"
    )


@pytest.mark.parametrize("builder_name", sorted(REGISTRY_BUILDERS))
async def test_order_writer_in_every_registry_refuses_instead_of_fabricating(
    builder_name: str,
) -> None:
    """Neither registry may hand back a created order it did not create.

    The stub this replaces minted ``ord-NNNN`` from a process counter and
    priced it off a five-product dict, so a customer was told their order
    existed while nothing was written. The platform ships no order system, so
    the only honest answer here is a refusal.

    Asserting the error alone would pass on a connector that returned an error
    AND an ``order_id``; the model reads the whole dict, so the absence of a
    success shape is the requirement.
    """
    import inspect

    spec = REGISTRY_BUILDERS[builder_name]().get("order_writer")

    result = spec.connector(
        {"client_id": "cl-001", "items": [{"product_id": "prod-001", "qty": 2}]}
    )
    if inspect.isawaitable(result):
        result = await result

    assert "order_id" not in result, (
        f"the {builder_name} registry's order_writer returned an order_id for "
        f"an order nothing persisted: {result!r}"
    )
    assert result.get("status") != "created"
    assert result["error_kind"] == "order_writing_not_configured"


@pytest.mark.parametrize("tool_name", FACTORY_BUILT_TOOLS)
def test_both_registries_build_factory_tools_from_the_same_factory(
    tool_name: str,
) -> None:
    """Factory-built tools leave SHARED_TOOLS, so their drift guard lives here.

    The identity check the shared test uses cannot work on a factory-built
    tool — each call closes over its own port and yields a distinct function.
    What must not drift is the contract the model sees, so compare that
    instead. If someone hand-writes a ToolSpec for one of these again, this
    fails.
    """
    stub_spec = _stub_registry().get(tool_name)
    rag_spec = _rag_registry().get(tool_name)

    assert stub_spec.required_permissions == rag_spec.required_permissions
    assert stub_spec.input_schema == rag_spec.input_schema
    assert stub_spec.description == rag_spec.description
