"""Tests for GET /webhook and POST /webhook endpoints (Meta WhatsApp Cloud API)."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock, patch

from badie.config import Settings, get_settings
from badie.main import create_app

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
        database_url="postgresql+asyncpg://localhost:5432/badie_test",
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

    from badie.integration.meta_signature import verify_signature

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

    from badie.integration.meta_signature import verify_signature

    body = b'{"hello": "world"}'
    headers = Headers({"x-hub-signature-256": "sha256=deadbeefdeadbeef"})
    with pytest.raises(HTTPException) as exc_info:
        verify_signature(body, headers, TEST_SECRET)
    assert exc_info.value.status_code == 403


def test_verify_signature_missing_header() -> None:
    """Missing X-Hub-Signature-256 header raises HTTPException(403)."""
    from fastapi import HTTPException
    from starlette.datastructures import Headers

    from badie.integration.meta_signature import verify_signature

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

    with patch("badie.integration.webhook.get_redis_client", return_value=mock_redis):
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

    with patch("badie.integration.webhook.get_redis_client", return_value=mock_redis):
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
    mock_redis.set.assert_called_once()


async def test_post_dedup_redis_failure(
    client: AsyncClient, text_payload: bytes
) -> None:
    """POST /webhook with Redis failure still processes message (fail-open)."""
    sig = sign_payload(text_payload, TEST_SECRET)
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))

    with patch("badie.integration.webhook.get_redis_client", return_value=mock_redis):
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
