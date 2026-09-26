"""Offline tests for the #78 Phase 0 Slice 3 live harness helpers -- the
ones that need no real model, Postgres, or Redis (`docs/platform/live-eval.md`
"Live harness" section). The harness itself (`boot_live_harness`) is only
exercised by the opt-in `tests/test_live_smoke.py` (`pytest -m live`).
"""

from __future__ import annotations

import httpx
import pytest

from agents_system.evals.live_harness import (
    AcceptAllParticipantDirectory,
    FakeWhatsAppServer,
    audit_rows_statement,
    build_meta_webhook_envelope,
    metric_value,
)
from agents_system.integration.meta_signature import sign_payload, verify_signature

# ---------------------------------------------------------------------------
# build_meta_webhook_envelope / sign_payload round trip
# ---------------------------------------------------------------------------


def test_build_meta_webhook_envelope_shape() -> None:
    envelope = build_meta_webhook_envelope(
        phone="+15550001111", text="hola", message_id="wamid.abc123"
    )

    message = envelope["entry"][0]["changes"][0]["value"]["messages"][0]
    assert message["id"] == "wamid.abc123"
    assert message["from"] == "+15550001111"
    assert message["type"] == "text"
    assert message["text"]["body"] == "hola"


def test_build_meta_webhook_envelope_round_trips_through_verify_signature() -> None:
    import json

    envelope = build_meta_webhook_envelope(
        phone="+15550001111", text="hola", message_id="wamid.abc123"
    )
    body = json.dumps(envelope).encode()
    secret = "a-test-secret"

    signature = sign_payload(body, secret)

    # Must not raise -- the harness signs with the exact same HMAC-SHA256
    # scheme the real `/webhook` route verifies with.
    verify_signature(body, httpx.Headers({"x-hub-signature-256": signature}), secret)


def test_build_meta_webhook_envelope_wrong_secret_is_rejected() -> None:
    import json

    from fastapi import HTTPException

    envelope = build_meta_webhook_envelope(
        phone="+15550001111", text="hola", message_id="wamid.abc123"
    )
    body = json.dumps(envelope).encode()
    signature = sign_payload(body, "right-secret")

    with pytest.raises(HTTPException) as exc_info:
        verify_signature(
            body, httpx.Headers({"x-hub-signature-256": signature}), "wrong-secret"
        )
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# FakeWhatsAppServer
# ---------------------------------------------------------------------------


async def test_fake_whatsapp_server_records_and_responds() -> None:
    server = FakeWhatsAppServer()
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            response = await client.post(
                "/test-phone-id/messages",
                json={
                    "messaging_product": "whatsapp",
                    "to": "+15550001111",
                    "type": "text",
                    "text": {"body": "hello from the harness"},
                },
                headers={"Authorization": "Bearer test-token"},
            )
    finally:
        await server.stop()

    assert response.status_code == 200
    assert response.json()["messages"][0]["id"] == "wamid.fake-0001"
    assert len(server.sent) == 1
    assert server.sent[0]["to"] == "+15550001111"
    assert server.sent[0]["text"]["body"] == "hello from the harness"


async def test_fake_whatsapp_server_records_every_send_in_order() -> None:
    server = FakeWhatsAppServer()
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=server.base_url) as client:
            for index in range(3):
                await client.post(
                    "/test-phone-id/messages",
                    json={
                        "messaging_product": "whatsapp",
                        "to": f"+1555000111{index}",
                        "type": "text",
                        "text": {"body": f"message {index}"},
                    },
                )
    finally:
        await server.stop()

    assert [entry["text"]["body"] for entry in server.sent] == [
        "message 0",
        "message 1",
        "message 2",
    ]


# ---------------------------------------------------------------------------
# metric_value
# ---------------------------------------------------------------------------

_SAMPLE_METRICS_TEXT = """\
# HELP agent_turns_total Total agent turns.
# TYPE agent_turns_total counter
agent_turns_total{runtime_id="live-harness",outcome="ok"} 3.0
agent_turns_total{runtime_id="live-harness",outcome="error"} 1.0
agent_turns_total{runtime_id="other-runtime",outcome="ok"} 9.0
"""


def test_metric_value_matches_exact_labels() -> None:
    value = metric_value(
        _SAMPLE_METRICS_TEXT,
        "agent_turns_total",
        runtime_id="live-harness",
        outcome="ok",
    )
    assert value == 3.0


def test_metric_value_distinguishes_label_values() -> None:
    value = metric_value(
        _SAMPLE_METRICS_TEXT,
        "agent_turns_total",
        runtime_id="live-harness",
        outcome="error",
    )
    assert value == 1.0


def test_metric_value_returns_none_for_missing_sample() -> None:
    value = metric_value(
        _SAMPLE_METRICS_TEXT,
        "agent_turns_total",
        runtime_id="unknown-runtime",
        outcome="ok",
    )
    assert value is None


def test_metric_value_returns_none_for_unknown_metric_name() -> None:
    value = metric_value(_SAMPLE_METRICS_TEXT, "not_a_real_metric")
    assert value is None


# ---------------------------------------------------------------------------
# audit_rows_statement
# ---------------------------------------------------------------------------


def test_audit_rows_statement_with_no_filters_has_no_where_clause() -> None:
    stmt = audit_rows_statement()
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "WHERE" not in compiled.upper()


def test_audit_rows_statement_filters_by_correlation_id() -> None:
    stmt = audit_rows_statement(correlation_id="abcd1234")
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "audit_event.correlation_id" in compiled
    assert "abcd1234" in compiled


def test_audit_rows_statement_combines_every_filter() -> None:
    from datetime import UTC, datetime

    stmt = audit_rows_statement(
        role="sales-agent",
        event_type="tool_call_attempted",
        tool_name="session_state",
        occurred_after=datetime(2026, 1, 1, tzinfo=UTC),
    )
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "audit_event.role" in compiled
    assert "sales-agent" in compiled
    assert "audit_event.event_type" in compiled
    assert "tool_call_attempted" in compiled
    assert "audit_event.tool_name" in compiled
    assert "session_state" in compiled
    assert "audit_event.occurred_at" in compiled


# ---------------------------------------------------------------------------
# AcceptAllParticipantDirectory
# ---------------------------------------------------------------------------


def test_accept_all_participant_directory_normalizes_address() -> None:
    directory = AcceptAllParticipantDirectory()
    assert directory.normalize_address("15550001111") == "+15550001111"
    assert directory.normalize_address("+15550001111") == "+15550001111"


def test_accept_all_participant_directory_rejects_empty_address() -> None:
    directory = AcceptAllParticipantDirectory()
    with pytest.raises(ValueError, match="empty"):
        directory.normalize_address("")


async def test_accept_all_participant_directory_resolves_to_active_participant() -> (
    None
):
    directory = AcceptAllParticipantDirectory()
    participant = await directory.resolve(session=None, address="+15550001111")
    assert participant.active is True
    assert participant.id
