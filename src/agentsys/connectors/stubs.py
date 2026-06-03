"""BADIE sales-agent connector stubs (D-006).

Deterministic fake connectors that return realistic data shapes for the five
sales-agent tools. Used for end-to-end harness testing without real external
dependencies (WhatsApp API, Postgres, Redis).

Each connector follows the same signature: dict[str, Any] -> dict[str, Any].
"""
from __future__ import annotations

import itertools
from typing import Any

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
    """Return a ToolRegistry wired with all five BADIE sales-agent stubs."""
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
    return registry
