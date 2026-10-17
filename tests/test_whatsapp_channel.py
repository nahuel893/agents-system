"""The WhatsApp/Meta adapter behind the channel port (#22).

Its behaviour is pinned end to end by the unchanged ``tests/test_webhook.py``
(route) and ``tests/test_webhook_worker.py`` (deferred worker). These tests
pin the adapter's own surface: that it satisfies the port, which Settings it
reads, and that each port method keeps the exact semantics the route and the
worker had before the extraction.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing
from fastapi import HTTPException
from starlette.datastructures import Headers

from agents_system.channels.port import (
    ChannelAdapter,
    InboundRef,
    InboundTurn,
    SubscriptionHandshake,
)
from agents_system.channels.whatsapp import (
    WHATSAPP_CHANNEL,
    WhatsAppChannel,
    WhatsAppChannelSettings,
)
from agents_system.config import Settings
from agents_system.integration.meta_signature import sign_payload

SECRET = "meta-app-secret"
VERIFY_TOKEN = "verify-token"


def _channel(**overrides: object) -> WhatsAppChannel:
    values: dict[str, object] = {
        "app_secret": SECRET,
        "verify_token": VERIFY_TOKEN,
        "access_token": "token",
        "phone_number_id": "123",
        "graph_api_url": "https://graph.example.test/v21.0",
        "runtime_id": "sales",
        "checkpointer_enabled": True,
    }
    values.update(overrides)
    return WhatsAppChannel(WhatsAppChannelSettings(**values))  # type: ignore[arg-type]


def _envelope(*messages: dict[str, object]) -> dict[str, object]:
    return {"entry": [{"changes": [{"value": {"messages": list(messages)}}]}]}


def test_the_whatsapp_channel_name_is_the_legacy_inbox_channel() -> None:
    from agents_system.models.outbox import LEGACY_CHANNEL

    assert WHATSAPP_CHANNEL == LEGACY_CHANNEL == "whatsapp"


def test_the_whatsapp_channel_satisfies_the_port() -> None:
    channel = _channel()

    assert isinstance(channel, ChannelAdapter)
    assert isinstance(channel, SubscriptionHandshake)


def test_the_settings_view_projects_only_the_whatsapp_fields() -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        allow_insecure=True,
        meta_webhook_secret="s",
        whatsapp_verify_token="v",
        whatsapp_token="t",
        whatsapp_phone_number_id="p",
        whatsapp_graph_api_url="https://graph.example.test/v1",
        whatsapp_runtime_id="r",
        whatsapp_checkpointer_enabled=False,
    )

    view = WhatsAppChannelSettings.from_settings(settings)

    assert view == WhatsAppChannelSettings(
        app_secret="s",
        verify_token="v",
        access_token="t",
        phone_number_id="p",
        graph_api_url="https://graph.example.test/v1",
        runtime_id="r",
        checkpointer_enabled=False,
    )


# ---------------------------------------------------------------------------
# verify_inbound -- the Meta HMAC, byte-for-byte
# ---------------------------------------------------------------------------


def test_verify_inbound_accepts_a_body_signed_with_the_app_secret() -> None:
    body = b'{"entry": []}'
    headers = Headers({"x-hub-signature-256": sign_payload(body, SECRET)})

    assert _channel().verify_inbound(body, headers) is None


@pytest.mark.parametrize(
    ("headers", "detail"),
    [
        ({}, "Missing signature header"),
        ({"x-hub-signature-256": "sha256=deadbeef"}, "Invalid signature"),
    ],
)
def test_verify_inbound_rejects_like_verify_signature(
    headers: dict[str, str], detail: str
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _channel().verify_inbound(b"{}", Headers(headers))

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == detail


def test_verify_inbound_never_verifies_against_another_secret() -> None:
    body = b"{}"
    headers = Headers({"x-hub-signature-256": sign_payload(body, "other-secret")})

    with pytest.raises(HTTPException):
        _channel().verify_inbound(body, headers)


# ---------------------------------------------------------------------------
# verify_subscription -- the GET verify-token handshake
# ---------------------------------------------------------------------------


def test_verify_subscription_echoes_the_challenge() -> None:
    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "abc",
    }

    assert _channel().verify_subscription(params) == "abc"


def test_verify_subscription_compares_the_token_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#165 review follow-up: a plain `==` on the verify token leaks timing
    information proportional to the number of matching leading bytes. The
    handshake must compare with `hmac.compare_digest` instead, exactly like
    the POST path's HMAC check (`verify_inbound`).
    """
    import hmac as hmac_module

    calls: list[tuple[bytes, bytes]] = []
    real_compare_digest = hmac_module.compare_digest

    def _spy(a: bytes, b: bytes) -> bool:
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(hmac_module, "compare_digest", _spy)

    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": VERIFY_TOKEN,
        "hub.challenge": "abc",
    }

    assert _channel().verify_subscription(params) == "abc"
    assert calls == [(VERIFY_TOKEN.encode("utf-8"), VERIFY_TOKEN.encode("utf-8"))]


@pytest.mark.parametrize(
    ("params", "status"),
    [
        ({"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN}, 400),
        # #182 -- no ``hub.verify_token`` at all: a 403, never a crash.
        ({"hub.mode": "subscribe", "hub.challenge": "c"}, 403),
        (
            {"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "c"},
            403,
        ),
        (
            {
                "hub.mode": "other",
                "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "c",
            },
            403,
        ),
    ],
)
def test_verify_subscription_rejects_like_the_old_route(
    params: dict[str, str], status: int
) -> None:
    with pytest.raises(HTTPException) as exc_info:
        _channel().verify_subscription(params)

    assert exc_info.value.status_code == status


# ---------------------------------------------------------------------------
# read_inbound / extract_turn -- Meta envelope normalization
# ---------------------------------------------------------------------------


def test_read_inbound_lists_every_message_of_a_batch_with_its_sender_key() -> None:
    envelope = _envelope(
        {"id": "wamid.1", "from": "5491100000001", "text": {"body": "a"}},
        {"id": "wamid.2", "text": {"body": "b"}},
        {"from": "5491100000001", "text": {"body": "no id"}},
    )

    delivery = _channel().read_inbound(json.dumps(envelope).encode())

    assert delivery.payload == envelope
    assert delivery.messages == (
        InboundRef(message_id="wamid.1", conversation_key="5491100000001"),
        InboundRef(message_id="wamid.2", conversation_key="unresolved:wamid.2"),
    )


@pytest.mark.parametrize(
    "body",
    [b"not-json", b'"a-string"', b'{"entry": [{"changes": [{"value": {}}]}]}'],
)
def test_read_inbound_has_no_messages_for_a_malformed_or_status_body(
    body: bytes,
) -> None:
    assert _channel().read_inbound(body).messages == ()


def test_extract_turn_finds_the_exact_message_inside_a_shared_envelope() -> None:
    envelope = _envelope(
        {"id": "wamid.1", "from": "111", "text": {"body": "first"}},
        {"id": "wamid.2", "from": "222", "text": {"body": "second"}},
    )

    assert _channel().extract_turn(envelope, "wamid.2") == InboundTurn(
        message_id="wamid.2", sender="222", text="second"
    )


def test_extract_turn_logs_and_skips_a_non_text_message() -> None:
    envelope = _envelope({"id": "wamid.1", "from": "111", "type": "image"})

    with structlog.testing.capture_logs() as logs:
        turn = _channel().extract_turn(envelope, "wamid.1")

    assert turn is None
    assert {
        "event": "webhook_worker.non_text_message",
        "meta_message_id": "wamid.1",
        "message_type": "image",
        "log_level": "info",
    } in logs


# ---------------------------------------------------------------------------
# send_text -- through the injected Graph API client
# ---------------------------------------------------------------------------


async def test_send_text_forwards_to_the_injected_sender() -> None:
    sender = MagicMock()
    sender.send_text = AsyncMock()

    await WhatsAppChannel(sender=sender).send_text(to="+1", body="hi")

    sender.send_text.assert_awaited_once_with(to="+1", body="hi")


async def test_send_text_without_a_sender_fails_loudly() -> None:
    with pytest.raises(RuntimeError):
        await _channel().send_text(to="+1", body="hi")


def test_inbound_verification_without_settings_fails_loudly() -> None:
    with pytest.raises(RuntimeError):
        WhatsAppChannel(sender=MagicMock()).verify_inbound(b"{}", Headers({}))


def test_verify_subscription_accepts_a_non_ascii_token() -> None:
    """The comparison is over UTF-8 bytes, so a non-ASCII secret works and
    never raises (``hmac.compare_digest`` rejects non-ASCII ``str``)."""
    channel = _channel(verify_token="tökén-ñandú-日本")

    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "tökén-ñandú-日本",
        "hub.challenge": "abc",
    }

    assert channel.verify_subscription(params) == "abc"


def test_verify_subscription_rejects_a_wrong_non_ascii_token() -> None:
    channel = _channel(verify_token="tökén")

    params = {
        "hub.mode": "subscribe",
        "hub.verify_token": "token",
        "hub.challenge": "abc",
    }

    with pytest.raises(HTTPException) as exc_info:
        channel.verify_subscription(params)

    assert exc_info.value.status_code == 403


def test_verify_subscription_never_matches_an_empty_configured_token() -> None:
    """An unset token must not make the empty string a valid credential,
    even where the boot validator was bypassed (``ALLOW_INSECURE``)."""
    channel = _channel(verify_token="")

    for supplied in ("", "anything"):
        params = {
            "hub.mode": "subscribe",
            "hub.verify_token": supplied,
            "hub.challenge": "abc",
        }
        with pytest.raises(HTTPException) as exc_info:
            channel.verify_subscription(params)
        assert exc_info.value.status_code == 403


@pytest.mark.parametrize(
    "payload",
    [
        {"entry": "nope"},
        {"entry": ["nope", {"changes": "nope"}, {"changes": ["nope"]}]},
        {"entry": [{"changes": [{"value": "nope"}]}]},
        {"entry": [{"changes": [{"value": {"messages": "nope"}}]}]},
        {"entry": [{"changes": [{"value": {"messages": ["nope"]}}]}]},
        {},
    ],
)
def test_a_malformed_envelope_yields_no_turn_instead_of_raising(
    payload: dict[str, object],
) -> None:
    assert _channel().extract_turn(payload, "wamid.1") is None


def test_a_non_text_message_completes_without_a_turn_and_is_logged() -> None:
    image = {"id": "wamid.1", "from": "549111", "type": "image"}

    with structlog.testing.capture_logs() as logs:
        turn = _channel().extract_turn(_envelope(image), "wamid.1")

    assert turn is None
    [entry] = [e for e in logs if e["event"] == "webhook_worker.non_text_message"]
    assert entry["message_type"] == "image"


def test_a_message_that_is_not_the_requested_id_is_skipped() -> None:
    other = {"id": "wamid.2", "from": "549111", "text": {"body": "hi"}}

    assert _channel().extract_turn(_envelope(other), "wamid.1") is None
