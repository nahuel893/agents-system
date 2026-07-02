"""Tests for GET /webhook and POST /webhook endpoints (Meta WhatsApp Cloud API)."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage

from agentsys.config import Settings, get_settings
from agentsys.main import create_app
from agentsys.models.tables import Client

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PAYLOADS_DIR = Path(__file__).parent / "payloads"

TEST_SECRET = "test_webhook_secret"
TEST_VERIFY_TOKEN = "test_verify_token"


def sign_payload(body: bytes, secret: str) -> str:
    """Return HMAC-SHA256 signature in Meta header format: 'sha256=<digest>'."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def make_settings(**overrides: str) -> Settings:
    """Build a Settings instance with sensible test defaults."""
    defaults = dict(
        meta_webhook_secret=TEST_SECRET,
        whatsapp_verify_token=TEST_VERIFY_TOKEN,
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
    )
    defaults.update(overrides)
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app():
    test_settings = make_settings()
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: test_settings
    return application


@pytest.fixture
async def client(app):
    # Provide a minimal engine mock so lifespan doesn't fail
    mock_engine = MagicMock()
    mock_engine.dispose = MagicMock(return_value=None)
    app.state.engine = mock_engine
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def text_payload() -> bytes:
    return (PAYLOADS_DIR / "text_message.json").read_bytes()


@pytest.fixture
def status_payload() -> bytes:
    return (PAYLOADS_DIR / "status_update.json").read_bytes()


# ---------------------------------------------------------------------------
# Phase 3 — verify_signature unit tests (tasks 2.3–2.5)
# ---------------------------------------------------------------------------


def test_verify_signature_valid() -> None:
    """Valid HMAC-SHA256 signature passes without raising."""
    from starlette.datastructures import Headers

    from agentsys.integration.meta_signature import verify_signature

    body = b'{"hello": "world"}'
    sig = sign_payload(body, TEST_SECRET)
    headers = Headers({"x-hub-signature-256": sig})
    # Must return None (no exception)
    result = verify_signature(body, headers, TEST_SECRET)
    assert result is None


def test_verify_signature_invalid() -> None:
    """Wrong HMAC digest raises HTTPException(403)."""
    from fastapi import HTTPException
    from starlette.datastructures import Headers

    from agentsys.integration.meta_signature import verify_signature

    body = b'{"hello": "world"}'
    headers = Headers({"x-hub-signature-256": "sha256=deadbeefdeadbeef"})
    with pytest.raises(HTTPException) as exc_info:
        verify_signature(body, headers, TEST_SECRET)
    assert exc_info.value.status_code == 403


def test_verify_signature_missing_header() -> None:
    """Missing X-Hub-Signature-256 header raises HTTPException(403)."""
    from fastapi import HTTPException
    from starlette.datastructures import Headers

    from agentsys.integration.meta_signature import verify_signature

    body = b'{"hello": "world"}'
    headers = Headers({})
    with pytest.raises(HTTPException) as exc_info:
        verify_signature(body, headers, TEST_SECRET)
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Phase 4 — GET /webhook handler tests (tasks 2.6–2.7)
# ---------------------------------------------------------------------------


async def test_get_challenge_valid(client: AsyncClient) -> None:
    """GET /webhook with correct token returns 200 and echoes the challenge."""
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": TEST_VERIFY_TOKEN,
            "hub.challenge": "challenge_value_abc123",
        },
    )
    assert response.status_code == 200
    assert response.text == "challenge_value_abc123"


async def test_get_challenge_wrong_token(client: AsyncClient) -> None:
    """GET /webhook with wrong token returns 403."""
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "WRONG_TOKEN",
            "hub.challenge": "challenge_value_abc123",
        },
    )
    assert response.status_code == 403


async def test_get_challenge_missing_challenge(client: AsyncClient) -> None:
    """GET /webhook without hub.challenge returns 400 or 403."""
    response = await client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": TEST_VERIFY_TOKEN,
        },
    )
    assert response.status_code in (400, 403)


# ---------------------------------------------------------------------------
# Phase 4 — POST /webhook handler tests (tasks 2.8–2.11)
# ---------------------------------------------------------------------------


async def test_post_text_message(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with valid sig + text message returns 200."""
    sig = sign_payload(text_payload, TEST_SECRET)
    response = await client.post(
        "/webhook",
        content=text_payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_post_status_update(
    client: AsyncClient, status_payload: bytes
) -> None:
    """POST /webhook with valid sig + status update returns 200 silently."""
    sig = sign_payload(status_payload, TEST_SECRET)
    response = await client.post(
        "/webhook",
        content=status_payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_post_invalid_signature(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with wrong signature returns 403."""
    response = await client.post(
        "/webhook",
        content=text_payload,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=wrongdigestdeadbeef",
        },
    )
    assert response.status_code == 403


async def test_post_missing_signature(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with no X-Hub-Signature-256 header returns 403."""
    response = await client.post(
        "/webhook",
        content=text_payload,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Phase 5 — Deduplication tests (tasks 2.1–2.3)
# ---------------------------------------------------------------------------


async def test_post_duplicate_message(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with duplicate message_id returns 200 but skips processing."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=None)  # key existed = duplicate

    with patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_post_new_message_with_dedup(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with new message_id processes normally."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # key created = new

    with patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_post_dedup_redis_failure(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with Redis failure still processes message (fail-open)."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))

    with patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Phase 6 — Client lookup integration tests (tasks 5.1–5.3)
# ---------------------------------------------------------------------------


async def test_post_unregistered_client(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with unregistered client returns 200 but skips processing."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # new message

    unregistered = Client(
        id=1, phone_number="+5491123456789", name="Pendiente de alta", active=False
    )
    mock_lookup = AsyncMock(return_value=unregistered)

    with (
        patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis),
        patch("agentsys.integration.webhook.lookup_or_create_client", mock_lookup),
    ):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    mock_lookup.assert_called_once()


# ---------------------------------------------------------------------------
# D-014 S1 — webhook -> AgentRuntime -> WhatsAppClient wiring
# ---------------------------------------------------------------------------


async def test_post_unresolved_runtime_no_run_turn_no_send(
    app, client: AsyncClient, text_payload: bytes
) -> None:
    """Unresolved whatsapp_runtime_id → {"status": "ok"}, no run_turn/send call."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # new message

    registered = Client(
        id=10, phone_number="+5491123456789", name="Kiosco Don José", active=True
    )
    mock_lookup = AsyncMock(return_value=registered)

    app.state.runtimes = {}  # nothing resolves
    fake_whatsapp_client = MagicMock()
    fake_whatsapp_client.send_text = AsyncMock()
    app.state.whatsapp_client = fake_whatsapp_client

    with (
        patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis),
        patch("agentsys.integration.webhook.lookup_or_create_client", mock_lookup),
    ):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    fake_whatsapp_client.send_text.assert_not_awaited()


async def test_post_resolved_runtime_invokes_run_turn_and_send(
    app, client: AsyncClient, text_payload: bytes
) -> None:
    """Resolved runtime → run_turn invoked, whatsapp_client.send_text called with reply."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    registered = Client(
        id=11, phone_number="+5491123456789", name="Kiosco Don José", active=True
    )
    mock_lookup = AsyncMock(return_value=registered)

    fake_runtime = MagicMock()
    fake_runtime.run_turn = AsyncMock(
        return_value=[AIMessage(content="Hola! Como puedo ayudarte?")]
    )
    app.state.runtimes = {"badie__sales-agent": fake_runtime}

    fake_whatsapp_client = MagicMock()
    fake_whatsapp_client.send_text = AsyncMock()
    app.state.whatsapp_client = fake_whatsapp_client

    with (
        patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis),
        patch("agentsys.integration.webhook.lookup_or_create_client", mock_lookup),
    ):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    fake_runtime.run_turn.assert_awaited_once()
    fake_whatsapp_client.send_text.assert_awaited_once()
    call_kwargs = fake_whatsapp_client.send_text.call_args.kwargs
    assert call_kwargs["body"] == "Hola! Como puedo ayudarte?"


async def test_post_send_failure_still_returns_200(
    app, client: AsyncClient, text_payload: bytes
) -> None:
    """Outbound send raising must not crash the webhook — always 200 to Meta."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    registered = Client(
        id=12, phone_number="+5491123456789", name="Kiosco Don José", active=True
    )
    mock_lookup = AsyncMock(return_value=registered)

    fake_runtime = MagicMock()
    fake_runtime.run_turn = AsyncMock(return_value=[AIMessage(content="reply")])
    app.state.runtimes = {"badie__sales-agent": fake_runtime}

    fake_whatsapp_client = MagicMock()
    fake_whatsapp_client.send_text = AsyncMock(side_effect=Exception("network down"))
    app.state.whatsapp_client = fake_whatsapp_client

    with (
        patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis),
        patch("agentsys.integration.webhook.lookup_or_create_client", mock_lookup),
    ):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_post_run_turn_failure_still_returns_200(
    app, client: AsyncClient, text_payload: bytes
) -> None:
    """Agent-turn failure (model backend down, connector error, etc.) must not
    crash the webhook — always 200 to Meta, and the send is skipped entirely
    (there is no reply to send)."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    registered = Client(
        id=14, phone_number="+5491123456789", name="Kiosco Don José", active=True
    )
    mock_lookup = AsyncMock(return_value=registered)

    fake_runtime = MagicMock()
    fake_runtime.run_turn = AsyncMock(side_effect=Exception("model down"))
    app.state.runtimes = {"badie__sales-agent": fake_runtime}

    fake_whatsapp_client = MagicMock()
    fake_whatsapp_client.send_text = AsyncMock()
    app.state.whatsapp_client = fake_whatsapp_client

    with (
        patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis),
        patch("agentsys.integration.webhook.lookup_or_create_client", mock_lookup),
    ):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    fake_whatsapp_client.send_text.assert_not_awaited()


async def test_post_write_tool_succeeds_with_default_permissions(
    app, client: AsyncClient, text_payload: bytes
) -> None:
    """Regression (discovery #184): a write:/send: tool call executes through
    the webhook entry point identically to the adapter entry point — the
    webhook must not force an empty permissions tuple."""
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    from agentsys.agent.graph import AgentRuntime
    from agentsys.harness.factory import EquippedRuntime
    from agentsys.harness.loader import AgentDefinition
    from agentsys.harness.registry import ToolSpec

    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)

    registered = Client(
        id=13, phone_number="+5491123456789", name="Kiosco Don José", active=True
    )
    mock_lookup = AsyncMock(return_value=registered)

    invoked: list[dict] = []

    def create_order(inputs: dict) -> dict:
        invoked.append(inputs)
        return {"order_id": "ord-002", "status": "created"}

    order_spec = ToolSpec(
        name="create_order",
        required_permissions=("write:orders",),
        connector=create_order,
        description="Create an order",
        input_schema={"type": "object", "properties": {}},
    )
    definition = AgentDefinition(
        role_name="sales-agent",
        version="1.0",
        deployment=None,
        system_prompt="You are a helpful assistant.",
        tools=(),
        skills=(),
        context={},
        permissions=("write:orders",),
        autonomy="supervised",
        escalation_rules={},
        delegation_policy={},
        memory_policy={},
        audit_policy={},
        execution_limits=None,
    )
    equipped = EquippedRuntime(
        definition=definition,
        system_prompt="You are a helpful assistant.",
        tools=(order_spec,),
        denied_tools=(),
        skills=(),
    )

    class _ToolAwareFakeModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):  # type: ignore[override]
            return self

    first_response = AIMessage(
        content="",
        tool_calls=[
            {"id": "call_1", "name": "create_order", "args": {}, "type": "tool_call"}
        ],
    )
    final_response = AIMessage(content="Order created.")
    model = _ToolAwareFakeModel(responses=[first_response, final_response])
    agent = AgentRuntime(equipped, model)

    app.state.runtimes = {"badie__sales-agent": agent}
    fake_whatsapp_client = MagicMock()
    fake_whatsapp_client.send_text = AsyncMock()
    app.state.whatsapp_client = fake_whatsapp_client

    with (
        patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis),
        patch("agentsys.integration.webhook.lookup_or_create_client", mock_lookup),
    ):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    # If the webhook forced permissions=(), the interceptor would raise
    # PolicyViolation before the connector ever runs — invoked would stay empty.
    assert len(invoked) == 1


async def test_post_registered_client(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with registered client processes normally."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # new message

    registered = Client(
        id=2, phone_number="+5491123456789", name="Kiosco Don José", active=True
    )
    mock_lookup = AsyncMock(return_value=registered)

    with (
        patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis),
        patch("agentsys.integration.webhook.lookup_or_create_client", mock_lookup),
    ):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_post_db_failure_fail_open(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with DB failure still processes message (fail-open)."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # new message

    mock_lookup = AsyncMock(side_effect=Exception("DB unavailable"))

    with (
        patch("agentsys.integration.webhook.get_redis_client", return_value=mock_redis),
        patch("agentsys.integration.webhook.lookup_or_create_client", mock_lookup),
    ):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
