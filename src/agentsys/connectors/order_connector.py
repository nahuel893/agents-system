"""Async `order_writer` connector (issue #39).

Replaces the stub in `connectors/stubs.py`, which computed a total from a
hardcoded five-product price list, minted `ord-NNNN` from a process counter,
and answered `status: created` for an order that was never written anywhere.
A customer was told their order existed; nothing existed.

The shape here is `report_connector`'s, and for the same reason: the tool
always exists, because `platform/roles/sales-agent` names it and a tool a
manifest names but the registry lacks makes the whole role unbuildable via
`InjectionError`. Whether a writer is bound behind it is a runtime fact the
tool reports — never one it papers over.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import structlog

from agentsys.harness.registry import Tier, ToolSpec
from agentsys.services.orders import OrderWriter

_logger = structlog.get_logger(__name__)

ConnectorOutput = dict[str, Any]
AsyncConnector = Callable[..., Awaitable[ConnectorOutput]]

_NOT_CONFIGURED_MESSAGE = (
    "Order creation is not available on this deployment — no order system is "
    "bound — so the order was NOT created and does not exist. Tell the "
    "customer plainly that you cannot place the order, and offer to hand them "
    "to a human. Do not invent an order number and do not imply the order was "
    "received."
)
"""What the tool answers when no writer is bound.

Written for the model, which is the only reader: it states the outcome (not
created), forbids the specific failure that made this issue (inventing an
order number), and gives a next action. It names no environment variable —
this text can reach a customer over WhatsApp, and operator configuration
detail does not belong there. Operators get the reason from the startup log.
"""

_WRITE_FAILED_MESSAGE = (
    "The order could not be saved, so it does NOT exist. Tell the customer "
    "the order was not placed and offer to retry or hand them to a human. Do "
    "not give them an order number."
)
"""Fixed text, deliberately.

Never interpolate the caught exception. A driver error stringifies the
statement plus connection detail, and for a URL-style DSN that includes the
password. It would land in the agent's message history, get summarized, and
be echoed to a customer. The real exception goes to the log.
"""

_UNCONFIRMED_MESSAGE = (
    "The order system did not return an order number, so this order could NOT "
    "be confirmed. Do NOT tell the customer the order was placed, and do NOT "
    "give them an order number. Do NOT place it again — it may already exist. "
    "Tell them it needs to be checked by a person and hand them to a human."
)
"""Deliberately not the write-failed text.

`create_order` returning normally asserts, per the protocol, that the order WAS
persisted; the platform simply cannot name it. Saying "not placed" here would
be a lie in the opposite direction, and the customer's natural response — order
again — would write a second real order. Unconfirmed is its own outcome and the
only safe instruction is to stop and escalate.
"""

_ORDER_WRITER_DESCRIPTION = (
    "Create a new order for a client. Requires client_id (from the client "
    "lookup tool) and a list of items with product_id and qty. An order "
    "exists only if this tool returns an order_id — if it returns an error, "
    "no order was created and you must say so."
)

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "client_id": {
            "type": "string",
            "description": "Client ID obtained from the client lookup tool",
        },
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "qty": {"type": "integer"},
                },
                "required": ["product_id", "qty"],
            },
            "description": "List of products to order",
        },
    },
    "required": ["client_id", "items"],
}


def build_order_writer_connector(writer: OrderWriter | None) -> AsyncConnector:
    """Build the async `order_writer` connector closure over *writer*.

    *writer* is the deployment's order system. `None` is a supported, and the
    default, state: the platform has no orders of its own.
    """

    async def order_writer_connector(
        inputs: dict[str, Any], *, session: Any = None
    ) -> ConnectorOutput:
        if writer is None:
            return {
                "error": _NOT_CONFIGURED_MESSAGE,
                "error_kind": "order_writing_not_configured",
            }

        client_id = str(inputs.get("client_id") or "").strip()
        if not client_id:
            return {
                "error": (
                    "No client_id was given, so no order was created. Look the "
                    "client up first, then place the order."
                ),
                "error_kind": "invalid_order",
            }

        items = inputs.get("items")
        if not isinstance(items, list) or not items:
            return {
                "error": (
                    "No items were given, so no order was created. An order "
                    "needs at least one product and quantity."
                ),
                "error_kind": "invalid_order",
            }

        try:
            result = await writer.create_order(
                session, client_id=client_id, items=items
            )
        except Exception:
            # A write failure has to come back as a RESULT, not an exception.
            # `_execute_tools` catches only TimeoutError and PolicyViolation,
            # so anything else escapes to whichever HTTP entry point is in
            # play — where the WhatsApp path returns 200 to Meta and the
            # customer simply never hears back, which is indistinguishable
            # from the order having been placed. The bare `Exception` is
            # deliberate: the writer belongs to the consuming application and
            # the platform cannot enumerate what it raises.
            _logger.exception("order.write_failed", client_id=client_id)
            return {
                "error": _WRITE_FAILED_MESSAGE,
                "error_kind": "order_write_failed",
            }

        # The writer belongs to the deployment, so its result is untrusted
        # input, not an internal value. This tool's own description promises
        # the model that "an order exists only if this tool returns an
        # order_id" — relaying `{"status": "created"}` with no id would make
        # that promise false and reinstate the exact fabrication this issue
        # removed, one boundary further out. The platform cannot verify the
        # write, so it does not repeat a claim about it.
        order_id = (
            str(result.get("order_id") or "").strip()
            if isinstance(result, dict)
            else ""
        )
        if not order_id:
            # The operator needs the broken contract; the model gets only the
            # safe sentence. Without this the writer stays silently
            # non-compliant and every order looks unconfirmed with no reason
            # recorded anywhere.
            _logger.error(
                "order.write_unconfirmed",
                client_id=client_id,
                result_type=type(result).__name__,
                result_keys=sorted(result) if isinstance(result, dict) else None,
            )
            return {
                "error": _UNCONFIRMED_MESSAGE,
                "error_kind": "order_write_unconfirmed",
            }

        return result

    return order_writer_connector


def build_order_writer_tool_spec(writer: OrderWriter | None) -> ToolSpec:
    """Return the `order_writer` ToolSpec, unregistered.

    Handed back loose rather than in a registry because the injector resolves
    every tool a role manifest names from ONE registry, and `sales-agent` asks
    for six tools of which this is one.
    """
    return ToolSpec(
        name="order_writer",
        description=_ORDER_WRITER_DESCRIPTION,
        required_permissions=("write:orders", "write:order_items"),
        input_schema=_INPUT_SCHEMA,
        connector=build_order_writer_connector(writer),
        tier=Tier.T2,
    )
