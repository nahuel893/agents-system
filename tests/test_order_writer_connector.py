"""The `order_writer` tool must never fabricate an order (issue #39).

The stub it replaces returned `{"order_id": "ord-0001", "status": "created"}`
without writing a row anywhere. That is the failure mode this file exists to
prevent: a connector that cannot do its job must SAY SO, the way `run_report`
does, instead of reporting success.
"""

from __future__ import annotations

from typing import Any

import pytest
from structlog.testing import capture_logs

from agentsys.connectors.order_connector import (
    _WRITE_FAILED_MESSAGE,
    build_order_writer_tool_spec,
)


class _RecordingWriter:
    """A minimal `OrderWriter` that records what it was asked to persist."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def create_order(
        self,
        session: Any,
        *,
        client_id: str,
        items: list[dict[str, Any]],
    ) -> Any:
        self.calls.append({"client_id": client_id, "items": items})
        return self.result


_AN_ORDER: dict[str, Any] = {
    "client_id": "cl-001",
    "items": [{"product_id": "prod-001", "qty": 2}],
}


async def test_an_unbound_order_writer_refuses_instead_of_fabricating() -> None:
    """No writer bound -> an error, and NOTHING that reads as a created order.

    Asserting the error alone would pass on a connector that returned both an
    error and an `order_id`; the model reads the whole dict, so the absence of
    a success shape is the real requirement.
    """
    spec = build_order_writer_tool_spec(None)

    result = await spec.connector(_AN_ORDER)

    assert result["error_kind"] == "order_writing_not_configured"
    assert "order_id" not in result
    assert result.get("status") != "created"


async def test_a_bound_order_writer_persists_through_the_injected_writer() -> None:
    """Bound -> the connector delegates; it does not compute the order itself."""
    writer = _RecordingWriter(
        {"order_id": "real-77", "status": "created", "total": 1700.0}
    )
    spec = build_order_writer_tool_spec(writer)

    result = await spec.connector(_AN_ORDER, session="the-session")

    assert result == {"order_id": "real-77", "status": "created", "total": 1700.0}
    assert writer.calls == [
        {"client_id": "cl-001", "items": [{"product_id": "prod-001", "qty": 2}]}
    ]


class _FailingWriter:
    """An `OrderWriter` whose backing store is down."""

    async def create_order(
        self,
        session: Any,
        *,
        client_id: str,
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        raise RuntimeError("connection refused: host=db.internal password=hunter2")


async def test_a_writer_that_raises_becomes_an_honest_failure_not_an_exception() -> (
    None
):
    """A raised write must come back as a RESULT saying the order does not exist.

    `_execute_tools` catches only TimeoutError and PolicyViolation, so anything
    else escapes to the HTTP entry point — and on the WhatsApp path that
    returns 200 to Meta with the customer never hearing back, which they cannot
    tell apart from a placed order.
    """
    spec = build_order_writer_tool_spec(_FailingWriter())

    result = await spec.connector(_AN_ORDER)

    assert result["error_kind"] == "order_write_failed"
    assert "order_id" not in result
    assert result.get("status") != "created"


async def test_a_write_failure_never_leaks_the_exception_text_to_the_model() -> None:
    """The error text reaches the model and from there a customer.

    A driver error stringifies the connection detail — host, user, and for a
    URL-style DSN the password. Interpolating it would put that in the agent's
    message history.
    """
    spec = build_order_writer_tool_spec(_FailingWriter())

    result = await spec.connector(_AN_ORDER)

    rendered = repr(result)
    assert "hunter2" not in rendered
    assert "db.internal" not in rendered
    assert "connection refused" not in rendered


@pytest.mark.parametrize(
    "result",
    [
        pytest.param({"status": "created", "total": 1700.0}, id="no-order-id"),
        pytest.param({"order_id": "", "status": "created"}, id="empty-order-id"),
        pytest.param({"order_id": "   ", "status": "created"}, id="blank-order-id"),
        pytest.param({"order_id": None, "status": "created"}, id="null-order-id"),
        pytest.param("created", id="not-a-dict"),
    ],
)
async def test_a_writer_result_without_an_order_id_is_not_reported_as_created(
    result: Any,
) -> None:
    """A success shape the platform cannot verify must not be passed upward.

    The connector's own description promises the model that "an order exists
    only if this tool returns an order_id". A deployment writer that returns
    `{"status": "created"}` with no id — or a blank one — would make that promise
    false and reinstate exactly the fabrication this issue removed, one boundary
    further out. The platform cannot verify the write, so it must not relay a
    claim about it.
    """
    spec = build_order_writer_tool_spec(_RecordingWriter(result))

    output = await spec.connector(_AN_ORDER)

    assert output["error_kind"] == "order_write_unconfirmed"
    assert "order_id" not in output
    assert output.get("status") != "created"


async def test_an_unconfirmed_write_does_not_tell_the_customer_it_failed() -> None:
    """Unconfirmed is not the same as failed, and conflating them costs money.

    `create_order` returning normally asserts, per the protocol, that the order
    WAS persisted — the connector just cannot name it. Reusing the write-failed
    text would tell the customer to place the order again, and the retry would
    write a second real order. The honest answer is that it could not be
    confirmed and a human must check.
    """
    spec = build_order_writer_tool_spec(_RecordingWriter({"status": "created"}))

    output = await spec.connector(_AN_ORDER)

    assert output["error"] != _WRITE_FAILED_MESSAGE
    assert "not placed" not in output["error"]


async def test_an_unconfirmed_write_is_logged_for_the_operator() -> None:
    """The model gets a safe sentence; the operator gets the broken contract.

    Without this the deployment's writer stays silently non-compliant: every
    order looks unconfirmed to the customer and nothing anywhere says why.
    """
    spec = build_order_writer_tool_spec(_RecordingWriter({"status": "created"}))

    with capture_logs() as logs:
        await spec.connector(_AN_ORDER)

    assert any(entry["event"] == "order.write_unconfirmed" for entry in logs)


@pytest.mark.parametrize(
    "inputs",
    [
        pytest.param({"items": [{"product_id": "p", "qty": 1}]}, id="no-client-id"),
        pytest.param({"client_id": "cl-001"}, id="no-items"),
        pytest.param({"client_id": "cl-001", "items": []}, id="empty-items"),
    ],
)
async def test_an_incomplete_order_is_rejected_before_the_writer_is_called(
    inputs: dict[str, Any],
) -> None:
    """Validation failures must not reach the writer, and must not look created."""
    writer = _RecordingWriter({"order_id": "should-not-happen", "status": "created"})
    spec = build_order_writer_tool_spec(writer)

    result = await spec.connector(inputs)

    assert "error" in result
    assert "order_id" not in result
    assert writer.calls == []
