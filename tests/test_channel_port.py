"""The channel port (#22): a second channel added without editing the platform.

Everything a channel owns -- authenticating a delivery, normalizing its
payload, sending the reply -- sits behind ``agents_system.channels.port``.
The fake channel below exists ONLY in this file: it is registered through
``create_app(channels=...)`` and nothing under ``src/`` knows about it.

The headline test drives one full turn through it: a real ``create_app``, a
real lifespan, the real generic ``POST /webhook/{channel}`` route, the real
``accept_inbound_message``, the real ``DeferredWebhookWorker`` and a real
``AgentRuntime`` built by the harness. Only what needs PostgreSQL (the
outbox's claim/lease/complete SQL) and the model provider are in-memory.
"""

from __future__ import annotations

import ast
import asyncio
import json
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import (
    FakeConversationRecorder,
    FakeParticipant,
    FakeParticipantDirectory,
    create_test_app,
)
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from langchain_core.language_models.fake_chat_models import (
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers

from agents_system.channels.port import (
    Channel,
    ChannelAdapter,
    InboundDelivery,
    InboundRef,
    InboundTurn,
)
from agents_system.config import Settings, get_settings
from agents_system.harness.loader import DefinitionError
from agents_system.models.outbox import InboundMessage, OutboxWork
from agents_system.services.outbox import (
    OutboxClaimOutcome,
    OutboxFailureOutcome,
    accept_inbound_message,
)
from agents_system.services.participants import ChannelParticipantDirectory
from agents_system.services.webhook_worker import ChannelBinding, DeferredWebhookWorker

#: ADR-010 (#222): these tests resolve concrete predefined roles by name, which
#: is deprecated. They move to `agent` or the reference agents in
#: `examples/agents/` when the roles leave `platform/roles/` (#229).
pytestmark = pytest.mark.filterwarnings(
    "ignore::agents_system.harness.loader.PredefinedRoleDeprecationWarning"
)

FAKE_TOKEN = "fake-channel-token"
FAKE_SENDER = "5491100000001"
FAKE_REPLY = "Hello from the fake channel"


class FakeChannel:
    """A complete second channel, defined only in this test module.

    Its one setting (the shared token) hangs off the adapter itself, not off
    the platform's ``Settings``.
    """

    def __init__(self, token: str) -> None:
        self._token = token
        self.sent: list[tuple[str, str]] = []

    def verify_inbound(self, body: bytes, headers: Headers) -> None:
        if headers.get("x-fake-token") != self._token:
            raise HTTPException(status_code=403, detail="Invalid fake token")

    def read_inbound(self, body: bytes) -> InboundDelivery:
        payload = json.loads(body)
        refs = tuple(
            InboundRef(message_id=message["id"], conversation_key=message["from"])
            for message in payload["messages"]
        )
        return InboundDelivery(payload=payload, messages=refs)

    def extract_turn(
        self, payload: dict[str, Any], message_id: str
    ) -> InboundTurn | None:
        for message in payload["messages"]:
            if message["id"] == message_id:
                return InboundTurn(
                    message_id=message["id"],
                    sender=message["from"],
                    text=message["text"],
                )
        return None

    async def send_text(self, to: str, body: str) -> None:
        self.sent.append((to, body))


class ChannelAwareDirectory:
    """A ``ChannelParticipantDirectory`` that keeps each channel's addresses
    in an identity space of their own: the same raw sender on two channels
    is two participants."""

    def __init__(self) -> None:
        self.normalized: list[tuple[str, str]] = []
        self.resolved: list[tuple[str, str]] = []

    def normalize_channel_address(self, raw: str, *, channel: str) -> str:
        self.normalized.append((channel, raw))
        return "+" + raw.lstrip("+")

    async def resolve_channel_address(
        self, session: Any, address: str, *, channel: str
    ) -> FakeParticipant:
        self.resolved.append((channel, address))
        return FakeParticipant(id=f"{channel}/{address}")


def _fake_body(*message_ids: str) -> bytes:
    return json.dumps(
        {
            "messages": [
                {"id": message_id, "from": FAKE_SENDER, "text": "hola"}
                for message_id in message_ids
            ]
        }
    ).encode()


class _ToolAwareFakeModel(FakeMessagesListChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        return self


# ---------------------------------------------------------------------------
# An in-memory stand-in for the two outbox tables
# ---------------------------------------------------------------------------


class _Store:
    """``webhook_inbox`` / ``outbox_work`` without PostgreSQL.

    The real ``accept_inbound_message`` writes through ``_StoreSession``;
    the claim/lease/complete SQL (``FOR UPDATE SKIP LOCKED``, database
    clock) is replaced by the smallest equivalent that hands each row out
    once.
    """

    def __init__(self) -> None:
        self.inbound: dict[uuid.UUID, InboundMessage] = {}
        self.work: dict[uuid.UUID, OutboxWork] = {}
        self.claimed: set[uuid.UUID] = set()
        self.completed: set[uuid.UUID] = set()
        self.failures: list[str] = []

    def session_factory(self) -> _StoreSession:
        return _StoreSession(self)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def claim(
            session: object, *, worker_id: str, limit: int, lease_duration: object
        ) -> OutboxClaimOutcome:
            ready = [w for w in self.work.values() if w.id not in self.claimed]
            ready = ready[:limit]
            self.claimed.update(w.id for w in ready)
            return OutboxClaimOutcome(claimed=ready, terminalized=[])

        async def persist(
            session: object,
            *,
            work: OutboxWork,
            worker_id: str,
            body: dict[str, Any],
            send_key: str,
        ) -> None:
            work.outbound_body = body
            work.outbound_send_key = send_key

        async def complete(
            session: object, *, work: OutboxWork, worker_id: str
        ) -> None:
            self.completed.add(work.id)

        async def record_failure(
            session: object, *, work: OutboxWork, worker_id: str, error: str
        ) -> OutboxFailureOutcome:
            self.failures.append(error)
            return OutboxFailureOutcome(terminal=False, alert_required=False)

        prefix = "agents_system.services.webhook_worker."
        monkeypatch.setattr(prefix + "claim_available_outbox_work", claim)
        monkeypatch.setattr(prefix + "persist_outbound_intent", persist)
        monkeypatch.setattr(prefix + "complete_outbox_work", complete)
        monkeypatch.setattr(prefix + "record_outbox_failure", record_failure)


class _StoreSession:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self._pending: list[InboundMessage | OutboxWork] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def add_all(self, rows: list[InboundMessage | OutboxWork]) -> None:
        self._pending.extend(rows)

    async def commit(self) -> None:
        for row in self._pending:
            if isinstance(row, InboundMessage):
                self._store.inbound[row.id] = row
            else:
                self._store.work[row.id] = row
        self._pending.clear()

    async def rollback(self) -> None:
        self._pending.clear()

    async def get(self, model: object, key: uuid.UUID) -> InboundMessage | None:
        assert model is InboundMessage
        return self._store.inbound.get(key)


async def _wait_for(condition: Callable[[], object], timeout_s: float = 10.0) -> None:
    async with asyncio.timeout(timeout_s):
        while not condition():
            await asyncio.sleep(0.01)


def _boot_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "allow_insecure": True,
        # Reaching the runtime loop needs no Redis this way.
        "whatsapp_checkpointer_enabled": False,
        "webhook_worker_poll_interval_s": 0.01,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _lifespan_patches(settings: Settings, store: _Store) -> list[Any]:
    audit_sink = MagicMock()
    audit_sink.return_value.start = AsyncMock()
    audit_sink.return_value.stop = AsyncMock()
    engine = MagicMock()
    engine.dispose = AsyncMock()
    return [
        patch("agents_system.main.get_settings", return_value=settings),
        patch("agents_system.main.get_engine", return_value=engine),
        patch("agents_system.audit.sink.AuditSink", audit_sink),
        patch("agents_system.main.close_redis_pool", new=AsyncMock()),
        patch(
            "agents_system.services.embeddings.get_embedding_provider",
            return_value=MagicMock(),
        ),
        patch(
            "agents_system.main.get_session_factory",
            return_value=store.session_factory,
        ),
        patch(
            "agents_system.integration.webhook.get_session_factory",
            return_value=store.session_factory,
        ),
    ]


# ---------------------------------------------------------------------------
# The proof: a second channel drives a full turn through the port
# ---------------------------------------------------------------------------


async def test_a_fake_second_channel_drives_a_full_turn_through_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import ExitStack

    from agents_system.main import lifespan

    store = _Store()
    store.install(monkeypatch)
    fake = FakeChannel(token=FAKE_TOKEN)
    recorder = FakeConversationRecorder()
    directory = ChannelAwareDirectory()
    settings = _boot_settings()
    app = create_test_app(
        agents={"support": "sales-agent"},
        grants={"support": ["read:catalog"]},
        channels={"fake": Channel(adapter=fake, runtime_id="support")},
        chat_model=_ToolAwareFakeModel(responses=[AIMessage(content=FAKE_REPLY)]),
        participant_directory=directory,
        conversation_recorder=recorder,
    )
    app.dependency_overrides[get_settings] = lambda: settings

    with ExitStack() as stack:
        for active in _lifespan_patches(settings, store):
            stack.enter_context(active)
        async with lifespan(app):
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/webhook/fake",
                    content=_fake_body("m-1"),
                    headers={"x-fake-token": FAKE_TOKEN},
                )
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            await _wait_for(lambda: store.completed)

    # The reply went out through the fake channel's own sender, to the
    # address the directory normalized -- told which channel it came from.
    assert fake.sent == [("+" + FAKE_SENDER, FAKE_REPLY)]
    assert directory.normalized == [("fake", FAKE_SENDER)]
    assert directory.resolved == [("fake", "+" + FAKE_SENDER)]
    # Durably accepted under the channel's own name, its ids scoped to it.
    (stored,) = store.inbound.values()
    assert stored.channel == "fake"
    assert stored.meta_message_id == "fake:m-1"
    (work,) = store.work.values()
    assert work.conversation_key == "fake:" + FAKE_SENDER
    assert work.outbound_send_key == "fake:m-1:reply"
    assert store.completed == {work.id}
    assert store.failures == []
    # The turn's conversation is scoped to the channel too.
    (turn,) = recorder.turns
    assert turn["thread_id"] == "fake:+" + FAKE_SENDER
    assert turn["participant_id"] == "fake/+" + FAKE_SENDER
    assert turn["assistant_text"] == FAKE_REPLY


# ---------------------------------------------------------------------------
# The generic route resolves its adapter
# ---------------------------------------------------------------------------


def _fake_app(fake: FakeChannel | None = None) -> Any:
    app = create_test_app(
        agents={"support": "sales-agent"},
        grants={"support": ["read:catalog"]},
        channels={
            "fake": Channel(
                adapter=fake or FakeChannel(token=FAKE_TOKEN), runtime_id="support"
            )
        },
    )
    settings = _boot_settings(meta_webhook_secret="meta-secret")
    app.dependency_overrides[get_settings] = lambda: settings
    app.state.engine = MagicMock()
    return app


async def _post(app: Any, path: str, body: bytes, headers: dict[str, str]) -> Any:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(path, content=body, headers=headers)


async def test_a_fake_channel_rejects_an_unauthenticated_delivery_before_persisting() -> (
    None
):
    accept = AsyncMock()
    with patch("agents_system.integration.webhook.accept_inbound_message", accept):
        response = await _post(
            _fake_app(), "/webhook/fake", _fake_body("m-1"), {"x-fake-token": "wrong"}
        )

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid fake token"}
    accept.assert_not_awaited()


async def test_the_route_persists_a_fake_delivery_under_its_channel_name() -> None:
    accept = AsyncMock()
    with (
        patch("agents_system.integration.webhook.accept_inbound_message", accept),
        patch(
            "agents_system.integration.webhook.get_session_factory",
            return_value=_Store().session_factory,
        ),
    ):
        response = await _post(
            _fake_app(),
            "/webhook/fake",
            _fake_body("m-1", "m-2"),
            {"x-fake-token": FAKE_TOKEN},
        )

    assert response.status_code == 200
    assert [c.kwargs["channel"] for c in accept.call_args_list] == ["fake", "fake"]
    assert [c.kwargs["meta_message_id"] for c in accept.call_args_list] == [
        "m-1",
        "m-2",
    ]


async def test_an_unknown_channel_is_not_found_and_never_persists() -> None:
    accept = AsyncMock()
    with patch("agents_system.integration.webhook.accept_inbound_message", accept):
        response = await _post(_fake_app(), "/webhook/nope", _fake_body("m-1"), {})

    assert response.status_code == 404
    accept.assert_not_awaited()


async def test_a_channel_without_a_subscription_handshake_answers_405() -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_fake_app()), base_url="http://test"
    ) as client:
        response = await client.get("/webhook/fake", params={"hub.challenge": "x"})

    assert response.status_code == 405


async def test_the_whatsapp_channel_is_also_reachable_by_its_name() -> None:
    app = _fake_app()
    settings = _boot_settings(whatsapp_verify_token="verify-me")
    app.dependency_overrides[get_settings] = lambda: settings
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/webhook/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "echo-this",
            },
        )

    assert response.status_code == 200
    assert response.text == "echo-this"


# ---------------------------------------------------------------------------
# verify_signature is reached through the port, not imported by the route
# ---------------------------------------------------------------------------


def test_the_webhook_route_never_imports_verify_signature() -> None:
    import agents_system.integration.webhook as webhook_module

    tree = ast.parse(Path(webhook_module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "verify_signature" not in imported
    assert "verify_signature" not in names
    assert not any(module.endswith("meta_signature") for module in modules)


async def test_the_meta_signature_check_is_reached_through_the_whatsapp_adapter() -> (
    None
):
    from agents_system.integration.meta_signature import (
        sign_payload,
        verify_signature,
    )

    spy = MagicMock(side_effect=verify_signature)
    body = b'{"entry": []}'
    with patch("agents_system.channels.whatsapp.verify_signature", spy):
        response = await _post(
            _fake_app(),
            "/webhook",
            body,
            {"X-Hub-Signature-256": sign_payload(body, "meta-secret")},
        )

    assert response.status_code == 200
    spy.assert_called_once()
    called_body, called_headers, called_secret = spy.call_args.args
    assert called_body == body
    assert called_headers["x-hub-signature-256"] == sign_payload(body, "meta-secret")
    assert called_secret == "meta-secret"


# ---------------------------------------------------------------------------
# Registration: create_app(channels=...) and boot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "Fake", "has space", "-fake", "a" * 33])
def test_create_app_rejects_an_invalid_channel_name(name: str) -> None:
    with pytest.raises(DefinitionError) as exc_info:
        create_test_app(
            channels={name: Channel(adapter=FakeChannel(FAKE_TOKEN), runtime_id="x")}
        )

    assert "channel" in str(exc_info.value)


def test_create_app_reserves_the_built_in_whatsapp_channel_name() -> None:
    with pytest.raises(DefinitionError) as exc_info:
        create_test_app(
            channels={
                "whatsapp": Channel(adapter=FakeChannel(FAKE_TOKEN), runtime_id="x")
            }
        )

    assert "'whatsapp'" in str(exc_info.value)


def test_create_app_rejects_a_value_that_is_not_a_channel_registration() -> None:
    with pytest.raises(DefinitionError) as exc_info:
        create_test_app(channels={"fake": FakeChannel(FAKE_TOKEN)})

    assert "Channel" in str(exc_info.value)


def test_create_app_rejects_an_adapter_that_does_not_implement_the_port() -> None:
    class _NoSend:
        def verify_inbound(self, body: bytes, headers: Headers) -> None: ...

    with pytest.raises(DefinitionError) as exc_info:
        create_test_app(
            channels={"fake": Channel(adapter=_NoSend(), runtime_id="x")}  # type: ignore[arg-type]
        )

    assert "ChannelAdapter" in str(exc_info.value)


def test_boot_fails_when_a_channel_names_an_unregistered_runtime() -> None:
    from agents_system.main import _boot_plan

    app = create_test_app(
        agents={"support": "sales-agent"},
        grants={"support": ["read:catalog"]},
        channels={"fake": Channel(adapter=FakeChannel(FAKE_TOKEN), runtime_id="ghost")},
    )

    with pytest.raises(DefinitionError) as exc_info:
        _boot_plan(app, _boot_settings())

    message = str(exc_info.value)
    assert "'ghost'" in message
    assert "fake" in message


async def test_boot_refuses_a_channel_bound_to_a_trusted_input_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ADR-002 C.13 holds for every webhook channel, not only WhatsApp."""
    from contextlib import ExitStack

    from agents_system.agent.spec import Agent
    from agents_system.main import lifespan

    store = _Store()
    settings = _boot_settings()
    app = create_test_app(
        agents={"internal": Agent(name="internal-bot")},
        grants={"internal": ["read:session"]},
        channels={
            "fake": Channel(adapter=FakeChannel(FAKE_TOKEN), runtime_id="internal")
        },
        chat_model=_ToolAwareFakeModel(responses=[]),
    )

    with ExitStack() as stack, pytest.raises(DefinitionError) as exc_info:
        for active in _lifespan_patches(settings, store):
            stack.enter_context(active)
        async with lifespan(app):
            pass

    message = str(exc_info.value)
    assert "'fake'" in message
    assert "'internal'" in message
    assert "untrusted_input" in message


def test_the_fake_channel_satisfies_the_port() -> None:
    assert isinstance(FakeChannel(FAKE_TOKEN), ChannelAdapter)


# ---------------------------------------------------------------------------
# Durable acceptance: a channel's ids and keys are scoped to it
# ---------------------------------------------------------------------------


class _AcceptSession:
    def __init__(self) -> None:
        self.added: list[InboundMessage | OutboxWork] = []

    def add_all(self, rows: list[InboundMessage | OutboxWork]) -> None:
        self.added.extend(rows)

    async def commit(self) -> None:
        return None


async def test_accept_scopes_a_named_channel_ids_and_conversation_keys() -> None:
    session = _AcceptSession()

    await accept_inbound_message(
        cast(AsyncSession, session),
        meta_message_id="m-1",
        payload={"messages": []},
        conversation_key=FAKE_SENDER,
        channel="fake",
    )

    inbound, work = session.added
    assert isinstance(inbound, InboundMessage)
    assert isinstance(work, OutboxWork)
    assert inbound.channel == "fake"
    assert inbound.meta_message_id == "fake:m-1"
    assert work.conversation_key == "fake:" + FAKE_SENDER


async def test_accept_keeps_whatsapp_ids_and_keys_exactly_as_meta_sent_them() -> None:
    session = _AcceptSession()

    await accept_inbound_message(
        cast(AsyncSession, session),
        meta_message_id="wamid.1",
        payload={"entry": []},
        conversation_key=FAKE_SENDER,
    )

    inbound, work = session.added
    assert isinstance(inbound, InboundMessage)
    assert isinstance(work, OutboxWork)
    assert inbound.channel == "whatsapp"
    assert inbound.meta_message_id == "wamid.1"
    assert work.conversation_key == FAKE_SENDER


# ---------------------------------------------------------------------------
# The worker dispatches each row to its own channel
# ---------------------------------------------------------------------------


async def test_a_row_whose_channel_has_no_binding_is_recorded_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _Store()
    store.install(monkeypatch)
    inbound = InboundMessage(
        id=uuid.uuid4(),
        channel="fake",
        meta_message_id="fake:m-1",
        payload=json.loads(_fake_body("m-1")),
    )
    store.inbound[inbound.id] = inbound
    work = OutboxWork(
        id=uuid.uuid4(), inbound_message_id=inbound.id, conversation_key="fake:x"
    )
    store.work[work.id] = work
    runtime = MagicMock()
    runtime.run_turn = AsyncMock()
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()

    worker = DeferredWebhookWorker(
        session_factory=store.session_factory,
        worker_id="worker-a",
        directory=MagicMock(),
        runtime=runtime,
        whatsapp_client=whatsapp,
    )
    await worker.process_available(limit=1)

    assert len(store.failures) == 1
    assert "fake" in store.failures[0]
    runtime.run_turn.assert_not_awaited()
    whatsapp.send_text.assert_not_awaited()
    assert store.completed == set()


def test_the_worker_needs_a_runtime_and_a_whatsapp_client_together() -> None:
    with pytest.raises(ValueError):
        DeferredWebhookWorker(
            session_factory=_Store().session_factory,
            worker_id="worker-a",
            directory=MagicMock(),
            runtime=MagicMock(),
        )


def test_the_worker_needs_at_least_one_channel() -> None:
    with pytest.raises(ValueError):
        DeferredWebhookWorker(
            session_factory=_Store().session_factory,
            worker_id="worker-a",
            directory=MagicMock(),
        )


# ---------------------------------------------------------------------------
# Participant identity is resolved per channel, never across channels
# ---------------------------------------------------------------------------


def _whatsapp_envelope(message_id: str, sender: str) -> dict[str, Any]:
    message = {"id": message_id, "from": sender, "text": {"body": "hola"}}
    return {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}


def _replying_runtime() -> MagicMock:
    runtime = MagicMock()
    runtime.run_turn = AsyncMock(return_value=[AIMessage(content="ok")])
    return runtime


def test_the_channel_aware_test_directory_satisfies_its_protocol() -> None:
    assert isinstance(ChannelAwareDirectory(), ChannelParticipantDirectory)
    assert not isinstance(FakeParticipantDirectory(), ChannelParticipantDirectory)


async def test_the_directory_is_told_which_channel_each_sender_arrived_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same raw sender on WhatsApp and on a second channel reaches the
    directory as two (channel, address) questions, so it can keep them apart
    -- a second channel cannot claim a WhatsApp customer's identity just by
    sending that customer's number."""
    store = _Store()
    store.install(monkeypatch)
    rows = [
        InboundMessage(
            id=uuid.uuid4(),
            channel="whatsapp",
            meta_message_id="wamid.1",
            payload=_whatsapp_envelope("wamid.1", FAKE_SENDER),
        ),
        InboundMessage(
            id=uuid.uuid4(),
            channel="fake",
            meta_message_id="fake:m-1",
            payload=json.loads(_fake_body("m-1")),
        ),
    ]
    for row in rows:
        store.inbound[row.id] = row
        work = OutboxWork(
            id=uuid.uuid4(), inbound_message_id=row.id, conversation_key=FAKE_SENDER
        )
        store.work[work.id] = work
    directory = ChannelAwareDirectory()
    recorder = FakeConversationRecorder()
    whatsapp = MagicMock()
    whatsapp.send_text = AsyncMock()

    worker = DeferredWebhookWorker(
        session_factory=store.session_factory,
        worker_id="worker-a",
        directory=directory,
        runtime=_replying_runtime(),
        whatsapp_client=whatsapp,
        recorder=recorder,
        channels={
            "fake": ChannelBinding(
                adapter=FakeChannel(FAKE_TOKEN), runtime=_replying_runtime()
            )
        },
    )
    await worker.process_available(limit=2)

    assert store.failures == []
    assert sorted(directory.normalized) == [
        ("fake", FAKE_SENDER),
        ("whatsapp", FAKE_SENDER),
    ]
    assert sorted(directory.resolved) == [
        ("fake", "+" + FAKE_SENDER),
        ("whatsapp", "+" + FAKE_SENDER),
    ]
    assert sorted(turn["participant_id"] for turn in recorder.turns) == [
        "fake/+" + FAKE_SENDER,
        "whatsapp/+" + FAKE_SENDER,
    ]


def test_the_worker_refuses_a_channel_blind_directory_for_another_channel() -> None:
    """A channel-blind ``ParticipantDirectory`` cannot tell a second
    channel's senders from WhatsApp's, so it may serve WhatsApp alone."""
    with pytest.raises(ValueError) as exc_info:
        DeferredWebhookWorker(
            session_factory=_Store().session_factory,
            worker_id="worker-a",
            directory=FakeParticipantDirectory(),
            runtime=_replying_runtime(),
            whatsapp_client=MagicMock(),
            channels={
                "fake": ChannelBinding(
                    adapter=FakeChannel(FAKE_TOKEN), runtime=_replying_runtime()
                )
            },
        )

    message = str(exc_info.value)
    assert "'fake'" in message
    assert "ChannelParticipantDirectory" in message


async def test_boot_refuses_a_registered_channel_with_a_channel_blind_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import ExitStack

    from agents_system.main import lifespan

    store = _Store()
    settings = _boot_settings()
    app = create_test_app(
        agents={"support": "sales-agent"},
        grants={"support": ["read:catalog"]},
        channels={
            "fake": Channel(adapter=FakeChannel(FAKE_TOKEN), runtime_id="support")
        },
        chat_model=_ToolAwareFakeModel(responses=[]),
        participant_directory=FakeParticipantDirectory(),
    )

    with ExitStack() as stack, pytest.raises(ValueError) as exc_info:
        for active in _lifespan_patches(settings, store):
            stack.enter_context(active)
        async with lifespan(app):
            pass

    assert "'fake'" in str(exc_info.value)
    assert "ChannelParticipantDirectory" in str(exc_info.value)
