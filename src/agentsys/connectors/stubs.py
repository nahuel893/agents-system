"""ACME sales-agent connector stubs (D-006).

Deterministic fake connectors that return realistic data shapes for the five
sales-agent tools. Used for end-to-end harness testing without real external
dependencies (WhatsApp API, Postgres, Redis).

Each connector follows the same signature: dict[str, Any] -> dict[str, Any].
"""

from __future__ import annotations

import itertools
from typing import Any

from agentsys.connectors.sales_reports import CATALOG as _SALES_REPORT_CATALOG
from agentsys.connectors.order_connector import build_order_writer_tool_spec
from agentsys.connectors.report_connector import build_report_tool_spec
from agentsys.connectors.platform_connectors import (
    build_conversation_summarizer_tool_spec,
    build_escalation_notifier_tool_spec,
    build_knowledge_retrieval_tool_spec,
)
from agentsys.connectors.operator import (
    TerminalPolicy,
    build_operator_tool_specs,
)
from agentsys.harness.registry import ToolRegistry, ToolSpec

_order_counter = itertools.count(1)
_msg_counter = itertools.count(1)

_CATALOG: list[dict[str, Any]] = [
    {"id": "prod-001", "name": "Azúcar La Colmena 1kg", "price": 850.0, "stock": 42},
    {"id": "prod-002", "name": "Harina 000 Pureza 1kg", "price": 720.0, "stock": 18},
    {"id": "prod-003", "name": "Aceite Natura 900ml", "price": 1350.0, "stock": 30},
    {"id": "prod-004", "name": "Arroz Largo Fino 1kg", "price": 680.0, "stock": 55},
    {"id": "prod-005", "name": "Yerba Amanda 500g", "price": 950.0, "stock": 24},
]

_CLIENTS: dict[str, dict[str, Any]] = {
    "5491112345678": {
        "client_id": "cl-001",
        "name": "Almacén Don Pedro",
        "phone": "5491112345678",
    },
    "5491187654321": {
        "client_id": "cl-002",
        "name": "Kiosco La Esquina",
        "phone": "5491187654321",
    },
}

_PRICE_BY_PRODUCT: dict[str, float] = {p["id"]: p["price"] for p in _CATALOG}


def catalog_search(inputs: dict[str, Any]) -> dict[str, Any]:
    q = (inputs.get("q") or "").lower()
    if not q:
        return {"results": _CATALOG}
    matches = [p for p in _CATALOG if q in p["name"].lower()]
    return {"results": matches if matches else _CATALOG[:2]}


def client_lookup(inputs: dict[str, Any]) -> dict[str, Any]:
    phone = str(inputs.get("phone", ""))
    client = _CLIENTS.get(phone)
    if client:
        return client
    return {"client_id": None, "name": None, "phone": phone}


# `order_writer` is deliberately NOT defined here (issue #39). It used to
# mint `ord-NNNN` from a process counter and price it off `_CATALOG`, which
# told a customer their order existed while nothing was written. The tool now
# comes from `connectors.order_connector`, unbound, and says so.


def message_sender(inputs: dict[str, Any]) -> dict[str, Any]:
    msg_id = f"stub-msg-{next(_msg_counter):04d}"
    return {"status": "sent", "message_id": msg_id, "to": inputs.get("to")}


def session_state(inputs: dict[str, Any]) -> dict[str, Any]:
    action = inputs.get("action", "get")
    session_id = inputs.get("session_id", "s-unknown")
    if action == "set":
        return {"status": "ok", "session_id": session_id}
    return {"session_id": session_id, "data": inputs.get("data", {})}


def build_acme_registry(
    terminal_policy: TerminalPolicy | None = None,
) -> ToolRegistry:
    """Return a ToolRegistry wired with the ACME sales-agent stubs plus the
    three platform-generic tools (knowledge_retrieval,
    conversation_summarizer, escalation_notifier) declared by the generic
    roles under ``platform/roles/``, all three registered unbound.

    Extra entries are inert for ACME: the injector only iterates over
    ``definition.tools``, so the sales-agent surface is unchanged.
    """
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="catalog_search",
            description="Search the product catalog. Returns a list of matching products with id, name, price, and stock.",
            required_permissions=("read:catalog",),
            input_schema={
                "type": "object",
                "properties": {
                    "q": {
                        "type": "string",
                        "description": "Search query (product name or keyword). Leave empty to return all products.",
                    }
                },
                "required": [],
            },
            connector=catalog_search,
        )
    )
    registry.register(
        ToolSpec(
            name="client_lookup",
            description="Look up a client by phone number. Returns client_id, name, and phone. Use this before creating an order.",
            required_permissions=("read:client_registry",),
            input_schema={
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Client phone number in international format, e.g. 5491112345678",
                    }
                },
                "required": ["phone"],
            },
            connector=client_lookup,
        )
    )
    # Unbound: it answers that the order was not created rather than being
    # absent. platform/roles/sales-agent names order_writer, and a tool a
    # manifest names but the registry lacks makes the whole role unbuildable
    # via InjectionError. A deployment with an order system binds a real
    # `OrderWriter` in its own registry.
    registry.register(build_order_writer_tool_spec(None))
    registry.register(
        ToolSpec(
            name="message_sender",
            description="Send a WhatsApp message to a phone number.",
            required_permissions=("send:message",),
            input_schema={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient phone number"},
                    "text": {"type": "string", "description": "Message text to send"},
                },
                "required": ["to", "text"],
            },
            connector=message_sender,
        )
    )
    registry.register(
        ToolSpec(
            name="session_state",
            description="Get or set session state data for the current conversation.",
            required_permissions=(),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "set"]},
                    "session_id": {"type": "string"},
                    "data": {"type": "object"},
                },
                "required": ["action", "session_id"],
            },
            connector=session_state,
        )
    )
    # Unbound (engine=None): it answers "reporting is not configured"
    # rather than being absent. platform/roles/data-agent names run_report,
    # and a tool a manifest names but the registry lacks makes the whole
    # role unbuildable via InjectionError — not partially usable. main.py
    # supplies the real read-only engine at startup.
    registry.register(build_report_tool_spec(None, _SALES_REPORT_CATALOG))
    # Unbound, like order_writer and run_report above: the platform owns no
    # knowledge base, no conversation store and no escalation channel, so each
    # tool reports that rather than answering (issue #39). The stubs these
    # replace returned three hardcoded knowledge hits, one fixed summary
    # sentence, and `esc-NNNN` for an escalation no human ever received.
    registry.register(build_knowledge_retrieval_tool_spec(None))
    registry.register(build_conversation_summarizer_tool_spec(None))
    registry.register(build_escalation_notifier_tool_spec(None))
    # The operator tools, registered INERT: `build_operator_tool_specs()` with
    # no policy refuses every command and roots reads at the process cwd.
    # `platform/roles/operator-agent` names both, and a tool a manifest names
    # but the registry lacks makes the whole role unbuildable -- so the choice
    # is between an inert tool and no operator role at all. An application
    # that wants real terminal access builds its own `TerminalPolicy` and
    # registers these specs itself.
    for spec in build_operator_tool_specs(terminal_policy):
        registry.register(spec)

    return registry
