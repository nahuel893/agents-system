"""Tests for GET /webhook and POST /webhook endpoints (Meta WhatsApp Cloud API).

W2b2 retired the synchronous route: POST /webhook now only verifies HMAC and
durably persists each valid Meta message before acknowledging. It never runs
a turn or sends a reply -- that moved to ``DeferredWebhookWorker`` and its own
tests in ``tests/test_webhook_worker.py``. The processing-behaviour assertions
that used to live here (dedup, participant lookup, run_turn, outbound send,
conversation logging, tool permissions) were ported there; what remains here
is the route's own new contract plus what never depended on processing at
all (signature verification, the GET challenge, module hygiene).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from agentsys.config import Settings, get_settings
from agentsys.services.outbox import InboundAcceptance
from conftest import create_test_app

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PAYLOADS_DIR = Path(__file__).parent / "payloads"

TEST_SECRET = "test_webhook_secret"
TEST_VERIFY_TOKEN = "test_verify_token"

# The Meta message id embedded in tests/payloads/text_message.json.
TEXT_PAYLOAD_MESSAGE_ID = "wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf"


def sign_payload(body: bytes, secret: str) -> str:
    """Return HMAC-SHA256 signature in Meta header format: 'sha256=<digest>'."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def make_settings(**overrides: str) -> Settings:
    """Build a Settings instance with sensible test defaults."""
    defaults = dict(
        meta_webhook_secret=TEST_SECRET,
        whatsapp_verify_token=TEST_VERIFY_TOKEN,
        whatsapp_runtime_id="acme__sales-agent",
        database_url="postgresql+asyncpg://localhost:5432/agentsys_test",
        redis_url="redis://localhost:6379/0",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_batch_payload(*message_ids: str) -> bytes:
    """A single signed envelope carrying several messages, Meta's own shape."""
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "5491123456789",
                                        "id": message_id,
                                        "timestamp": "1700000000",
                                        "text": {"body": "hola"},
                                    }
                                    for message_id in message_ids
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()


def _dummy_session_factory() -> MagicMock:
    """A ``get_session_factory`` double whose sessions are never inspected.

    Every persistence assertion in this file goes through the patched
    ``accept_inbound_message`` instead, so the session this yields only needs
    to support ``async with ... as session``.
    """
    session_cm = AsyncMock()
    session_cm.__aenter__ = AsyncMock(return_value=MagicMock())
    session_cm.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=session_cm)


def _patch_persistence(accept: AsyncMock):
    """Patch both symbols the route needs to reach ``accept_inbound_message``."""
    return (
        patch("agentsys.integration.webhook.accept_inbound_message", accept),
        patch(
            "agentsys.integration.webhook.get_session_factory",
            return_value=_dummy_session_factory(),
        ),
    )


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
    application = create_test_app()
    application.dependency_overrides[get_settings] = lambda: test_settings
    return application


@pytest.fixture
async def client(app):
    # Provide a minimal engine mock so lifespan doesn't fail
    mock_engine = MagicMock()
    mock_engine.dispose = MagicMock(return_value=None)
    app.state.engine = mock_engine
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


@pytest.fixture
def text_payload() -> bytes:
    return (PAYLOADS_DIR / "text_message.json").read_bytes()


@pytest.fixture
def status_payload() -> bytes:
    return (PAYLOADS_DIR / "status_update.json").read_bytes()


# ---------------------------------------------------------------------------
# verify_signature unit tests
# ---------------------------------------------------------------------------


def test_verify_signature_valid() -> None:
    """Valid HMAC-SHA256 signature passes without raising."""
    from starlette.datastructures import Headers

    from agentsys.integration.meta_signature import verify_signature

    body = b'{"hello": "world"}'
    sig = sign_payload(body, TEST_SECRET)
    headers = Headers({"x-hub-signature-256": sig})
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
# GET /webhook handshake
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
# POST /webhook -- HMAC first, before any persistence (behaviour e)
# ---------------------------------------------------------------------------


async def test_post_invalid_signature_never_touches_persistence(
    client: AsyncClient, text_payload: bytes
) -> None:
    """A forged signature is rejected with 403 and never reaches the DB."""
    accept = AsyncMock()
    with patch("agentsys.integration.webhook.accept_inbound_message", accept):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": "sha256=wrongdigestdeadbeef",
            },
        )

    assert response.status_code == 403
    accept.assert_not_awaited()


async def test_post_missing_signature_never_touches_persistence(
    client: AsyncClient, text_payload: bytes
) -> None:
    """No X-Hub-Signature-256 header at all is rejected the same way."""
    accept = AsyncMock()
    with patch("agentsys.integration.webhook.accept_inbound_message", accept):
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 403
    accept.assert_not_awaited()


# ---------------------------------------------------------------------------
# POST /webhook -- persist before ack, no turn in-request (behaviour a)
# ---------------------------------------------------------------------------


async def test_post_persists_the_message_before_ack_and_runs_no_turn(
    client: AsyncClient, text_payload: bytes
) -> None:
    """A validly signed message is committed via ``accept_inbound_message``
    before the 200. app.state carries no runtime and no WhatsApp client in
    this test at all -- the request still succeeds, which is the load-bearing
    proof that nothing in this handler reaches for either."""
    sig = sign_payload(text_payload, TEST_SECRET)
    accept = AsyncMock(
        return_value=InboundAcceptance(
            inbound_message_id=uuid.uuid4(), duplicate=False
        )
    )

    p1, p2 = _patch_persistence(accept)
    with p1, p2:
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
    accept.assert_awaited_once()
    call_kwargs = accept.call_args.kwargs
    assert call_kwargs["meta_message_id"] == TEXT_PAYLOAD_MESSAGE_ID
    assert call_kwargs["payload"] == json.loads(text_payload)


async def test_post_status_update_is_not_persisted(
    client: AsyncClient, status_payload: bytes
) -> None:
    """A status-update event (no `messages` array) touches no persistence."""
    sig = sign_payload(status_payload, TEST_SECRET)
    accept = AsyncMock()

    with patch("agentsys.integration.webhook.accept_inbound_message", accept):
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
    accept.assert_not_awaited()


async def test_post_skips_a_message_with_no_id_but_persists_the_rest(
    client: AsyncClient,
) -> None:
    """A message missing/blank `id` is not a valid Meta message to persist;
    a sibling in the same array with a real id still is."""
    payload = json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "from": "5491123456789",
                                        "id": "",
                                        "text": {"body": "no id"},
                                    },
                                    {
                                        "from": "5491123456789",
                                        "id": "wamid.only-valid",
                                        "text": {"body": "has id"},
                                    },
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()
    sig = sign_payload(payload, TEST_SECRET)
    accept = AsyncMock(
        return_value=InboundAcceptance(
            inbound_message_id=uuid.uuid4(), duplicate=False
        )
    )

    p1, p2 = _patch_persistence(accept)
    with p1, p2:
        response = await client.post(
            "/webhook",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    accept.assert_awaited_once()
    assert accept.call_args.kwargs["meta_message_id"] == "wamid.only-valid"


# ---------------------------------------------------------------------------
# POST /webhook -- 503 on persistence failure (behaviour b)
# ---------------------------------------------------------------------------


async def test_post_persistence_failure_returns_503(
    client: AsyncClient, text_payload: bytes
) -> None:
    """A commit failure (e.g. a transient DB error) never becomes a 200 --
    Meta must retry, not treat an unpersisted message as delivered."""
    sig = sign_payload(text_payload, TEST_SECRET)
    accept = AsyncMock(side_effect=RuntimeError("db down"))

    p1, p2 = _patch_persistence(accept)
    with p1, p2:
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 503


async def test_post_stops_after_the_first_persistence_failure_in_a_batch(
    client: AsyncClient,
) -> None:
    """In a 3-message batch where the second commit fails, the handler stops
    there: the third message is never attempted in this request. Meta's
    retry of the whole envelope is what reaches it, once the failure clears."""
    payload = make_batch_payload("wamid.1", "wamid.2", "wamid.3")
    sig = sign_payload(payload, TEST_SECRET)
    accept = AsyncMock(
        side_effect=[
            InboundAcceptance(inbound_message_id=uuid.uuid4(), duplicate=False),
            RuntimeError("db down"),
        ]
    )

    p1, p2 = _patch_persistence(accept)
    with p1, p2:
        response = await client.post(
            "/webhook",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 503
    assert accept.await_count == 2
    assert accept.call_args_list[0].kwargs["meta_message_id"] == "wamid.1"
    assert accept.call_args_list[1].kwargs["meta_message_id"] == "wamid.2"


# ---------------------------------------------------------------------------
# POST /webhook -- idempotent retry of an already-committed sibling
# (behaviour c: a Meta retry of the same message id creates no second turn --
# the route no longer runs turns at all, and a committed id simply replays)
# ---------------------------------------------------------------------------


async def test_post_duplicate_meta_message_id_is_accepted_idempotently(
    client: AsyncClient, text_payload: bytes
) -> None:
    """A retried message id that ``accept_inbound_message`` recognizes as
    already committed (``duplicate=True``) is not an error -- it acks 200
    without a second work row being created (accept_inbound_message's own
    contract, exercised here through its caller)."""
    sig = sign_payload(text_payload, TEST_SECRET)
    accept = AsyncMock(
        return_value=InboundAcceptance(
            inbound_message_id=uuid.uuid4(), duplicate=True
        )
    )

    p1, p2 = _patch_persistence(accept)
    with p1, p2:
        response = await client.post(
            "/webhook",
            content=text_payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    accept.assert_awaited_once()


async def test_post_retry_after_partial_failure_replays_the_committed_sibling(
    client: AsyncClient,
) -> None:
    """After the batch-failure scenario above, Meta retries the same
    envelope: the first message (already committed) comes back
    ``duplicate=True`` instead of raising, and the previously-failing second
    message is attempted again and now succeeds."""
    payload = make_batch_payload("wamid.1", "wamid.2")
    sig = sign_payload(payload, TEST_SECRET)
    accept = AsyncMock(
        side_effect=[
            InboundAcceptance(inbound_message_id=uuid.uuid4(), duplicate=True),
            InboundAcceptance(inbound_message_id=uuid.uuid4(), duplicate=False),
        ]
    )

    p1, p2 = _patch_persistence(accept)
    with p1, p2:
        response = await client.post(
            "/webhook",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    assert accept.await_count == 2


# ---------------------------------------------------------------------------
# POST /webhook -- malformed/attacker-controlled bodies never 500
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        b'{"entry": "not-a-list"}',
        b'{"entry": [{"changes": 7}]}',
        b'{"entry": [{"changes": [{"value": "not-a-dict"}]}]}',
        b'{"entry": null}',
        b"not-json-at-all",
        b'"just-a-json-string"',
    ],
)
async def test_a_malformed_payload_never_500s_and_persists_nothing(
    client: AsyncClient, body: bytes
) -> None:
    """Both the JSON parse and the payload-shape walk are attacker-controlled
    input; neither may turn into an unhandled 500 (Meta retries a 5xx
    forever) or an attempted persistence call."""
    sig = sign_payload(body, TEST_SECRET)
    accept = AsyncMock()

    with patch("agentsys.integration.webhook.accept_inbound_message", accept):
        response = await client.post(
            "/webhook",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )

    assert response.status_code == 200
    accept.assert_not_awaited()


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------


def test_webhook_module_does_not_import_client_domain() -> None:
    """The inbound route must not reach into any client-owned module."""
    import agentsys.integration.webhook as webhook_module

    source = Path(webhook_module.__file__).read_text(encoding="utf-8")

    assert "services.clients" not in source
    assert "services.conversation_log" not in source


def test_webhook_module_does_not_import_client_domain_at_runtime() -> None:
    """The substring scan above cannot see a transitive or aliased import.

    Run in a fresh interpreter so no other test's imports leak into
    sys.modules and make this vacuously pass — the same probe pattern
    `test_public_api.py` uses, and for the same reason.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import agentsys.integration.webhook\n"
            "leaked = [m for m in sys.modules if m in {\n"
            "    'agentsys.services.clients',\n"
            "    'agentsys.services.conversation_log',\n"
            "}]\n"
            "assert not leaked, leaked\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_the_wired_implementations_satisfy_the_ports() -> None:
    """The test doubles wired on create_test_app satisfy the ports.

    A Protocol is structural, so a missing or misnamed method is invisible to
    mypy at the wiring site and to every test that installs a fake instead.
    These ports are consumed by ``DeferredWebhookWorker`` now, not by this
    route -- kept here because it is about ``create_test_app``'s wiring, not
    about the webhook module.
    """
    import inspect

    app = create_test_app()
    directory = app.state.participant_directory
    assert directory is not None
    assert callable(directory.normalize_address)
    assert inspect.iscoroutinefunction(directory.resolve)
    assert set(inspect.signature(directory.resolve).parameters) >= {
        "session",
        "address",
    }

    recorder = app.state.conversation_recorder
    assert recorder is not None
    assert inspect.iscoroutinefunction(recorder.record_turn)
    assert set(inspect.signature(recorder.record_turn).parameters) >= {
        "session",
        "thread_id",
        "participant_id",
        "user_text",
        "assistant_text",
    }


def test_webhook_does_not_pull_in_client_orm_models() -> None:
    """Pins a leak the substring scan above cannot see.

    ``agentsys.models.tables`` — ACME's clients/orders/catalog_embeddings ORM
    models — no longer exists (#70): ``webhook.py`` imports
    ``agentsys.models.base``, and ``models/__init__.py`` used to eagerly
    re-export ``models.tables`` so that ``Base.metadata`` held every table.
    Now it only re-exports the platform's own ``audit_event``, so this
    boundary holds for real instead of by strict xfail.
    """
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import agentsys.integration.webhook\n"
            "assert 'agentsys.models.tables' not in sys.modules\n",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(Path(__file__).resolve().parents[1]),
    )

    assert result.returncode == 0, result.stdout + result.stderr
