"""BADIE sales-agent connector stubs (D-006).

Deterministic fake connectors that return realistic data shapes for the five
sales-agent tools. Used for end-to-end harness testing without real external
dependencies (WhatsApp API, Postgres, Redis).

Each connector follows the same signature: dict[str, Any] -> dict[str, Any].
"""
from __future__ import annotations

import itertools
from typing import Any

from agentsys.connectors.badie_reports import CATALOG as _BI_CATALOG
from agentsys.connectors.report_connector import build_report_tool_spec
from agentsys.connectors.platform_stubs import (
    conversation_summarizer,
    escalation_notifier,
    knowledge_retrieval,
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
    "5491112345678": {"client_id": "cl-001", "name": "Almacén Don Pedro", "phone": "5491112345678"},
    "5491187654321": {"client_id": "cl-002", "name": "Kiosco La Esquina", "phone": "5491187654321"},
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


def order_writer(inputs: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = inputs.get("items", [])
    total = sum(
        _PRICE_BY_PRODUCT.get(item.get("product_id", ""), 0.0) * item.get("qty", 1)
        for item in items
    )
    order_id = f"ord-{next(_order_counter):04d}"
    return {"order_id": order_id, "status": "created", "total": float(total)}


def message_sender(inputs: dict[str, Any]) -> dict[str, Any]:
    msg_id = f"stub-msg-{next(_msg_counter):04d}"
    return {"status": "sent", "message_id": msg_id, "to": inputs.get("to")}


def session_state(inputs: dict[str, Any]) -> dict[str, Any]:
    action = inputs.get("action", "get")
    session_id = inputs.get("session_id", "s-unknown")
    if action == "set":
        return {"status": "ok", "session_id": session_id}
    return {"session_id": session_id, "data": inputs.get("data", {})}


def build_badie_registry() -> ToolRegistry:
    """Return a ToolRegistry wired with the five BADIE sales-agent stubs plus
    the three platform-generic stubs (knowledge_retrieval,
    conversation_summarizer, escalation_notifier) declared by the generic
    roles under ``platform/roles/``.

    Extra entries are inert for BADIE: the injector only iterates over
    ``definition.tools``, so the sales-agent surface is unchanged.
    """
    registry = ToolRegistry()
    registry.register(ToolSpec(
        name="catalog_search",
        description="Search the product catalog. Returns a list of matching products with id, name, price, and stock.",
        required_permissions=("read:catalog",),
        input_schema={"type": "object", "properties": {"q": {"type": "string", "description": "Search query (product name or keyword). Leave empty to return all products."}}, "required": []},
        connector=catalog_search,
    ))
    registry.register(ToolSpec(
        name="client_lookup",
        description="Look up a client by phone number. Returns client_id, name, and phone. Use this before creating an order.",
        required_permissions=("read:client_registry",),
        input_schema={"type": "object", "properties": {"phone": {"type": "string", "description": "Client phone number in international format, e.g. 5491112345678"}}, "required": ["phone"]},
        connector=client_lookup,
    ))
    registry.register(ToolSpec(
        name="order_writer",
        description="Create a new order for a client. Requires client_id (from client_lookup) and a list of items with product_id and qty.",
        required_permissions=("write:orders", "write:order_items"),
        input_schema={"type": "object", "properties": {"client_id": {"type": "string", "description": "Client ID obtained from client_lookup"}, "items": {"type": "array", "items": {"type": "object", "properties": {"product_id": {"type": "string"}, "qty": {"type": "integer"}}, "required": ["product_id", "qty"]}, "description": "List of products to order"}}, "required": ["client_id", "items"]},
        connector=order_writer,
    ))
    registry.register(ToolSpec(
        name="message_sender",
        description="Send a WhatsApp message to a phone number.",
        required_permissions=("send:message",),
        input_schema={"type": "object", "properties": {"to": {"type": "string", "description": "Recipient phone number"}, "text": {"type": "string", "description": "Message text to send"}}, "required": ["to", "text"]},
        connector=message_sender,
    ))
    registry.register(ToolSpec(
        name="session_state",
        description="Get or set session state data for the current conversation.",
        required_permissions=(),
        input_schema={"type": "object", "properties": {"action": {"type": "string", "enum": ["get", "set"]}, "session_id": {"type": "string"}, "data": {"type": "object"}}, "required": ["action", "session_id"]},
        connector=session_state,
    ))
    # Unbound (engine=None): it answers "reporting is not configured"
    # rather than being absent. platform/roles/data-agent names run_report,
    # and a tool a manifest names but the registry lacks makes the whole
    # role unbuildable via InjectionError — not partially usable. main.py
    # supplies the real read-only engine at startup.
    registry.register(build_report_tool_spec(None, _BI_CATALOG))
    registry.register(ToolSpec(
        name="knowledge_retrieval",
        description="Search the organizational knowledge base. Returns a list of matching knowledge hits with id, title, and snippet.",
        required_permissions=("read:knowledge_base",),
        input_schema={"type": "object", "properties": {"q": {"type": "string", "description": "Natural-language knowledge query, e.g. 'return policy' or 'delivery zones'"}}, "required": ["q"]},
        connector=knowledge_retrieval,
    ))
    registry.register(ToolSpec(
        name="conversation_summarizer",
        description="Summarize a conversation session. Returns the session_id, a summary text, and message_count. Use max_messages to bound how many recent messages are considered.",
        required_permissions=("read:conversation_logs",),
        input_schema={"type": "object", "properties": {"session_id": {"type": "string", "description": "Conversation session identifier"}, "max_messages": {"type": "integer", "description": "Optional cap on the number of most recent messages to summarize"}}, "required": ["session_id"]},
        connector=conversation_summarizer,
    ))
    registry.register(ToolSpec(
        name="escalation_notifier",
        description="Notify a human operator that the conversation needs escalation. Requires a reason and supporting details. Returns the notification status and escalation_id.",
        required_permissions=("send:escalation",),
        input_schema={"type": "object", "properties": {"reason": {"type": "string", "description": "Short reason for the escalation, e.g. 'customer_angry'"}, "details": {"type": "string", "description": "Supporting context for the human operator"}}, "required": ["reason", "details"]},
        connector=escalation_notifier,
    ))
    return registry
