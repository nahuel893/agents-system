# Channels: adding a messaging channel behind the port

Issue #22. A **channel** is how a conversation reaches the platform and how
the reply leaves it: WhatsApp today, Telegram or an SMS provider tomorrow.
A channel does three things and nothing else:

1. **Authenticate** an inbound delivery (Meta signs with an HMAC header;
   another provider may send a shared secret token).
2. **Normalize** the payload into the messages the durable inbox accepts,
   and later into the one turn the agent runs.
3. **Send** a reply back out.

Everything between those ends is channel-agnostic and belongs to the
platform: the bounded body read, durable acceptance and dedup, the deferred
worker, participant lookup, running the turn, recording it, retries and
terminal failures. The port is `src/agents_system/channels/port.py`; the
built-in WhatsApp channel (`src/agents_system/channels/whatsapp.py`) is its
first adapter. Adding a channel edits no platform module.

---

## The port

```python
class ChannelAdapter(Protocol):
    def verify_inbound(self, body: bytes, headers: Headers) -> None: ...
    def read_inbound(self, body: bytes) -> InboundDelivery: ...
    def extract_turn(
        self, payload: dict[str, Any], message_id: str
    ) -> InboundTurn | None: ...
    async def send_text(self, to: str, body: str) -> None: ...
```

| Method | Runs | Contract |
|---|---|---|
| `verify_inbound` | In the request, on the raw body, after the size ceiling and **before anything is parsed or persisted** | Reject by raising `fastapi.HTTPException` (normally 403). Compare secrets in constant time (`hmac.compare_digest` / `secrets.compare_digest`). |
| `read_inbound` | In the request, only after `verify_inbound` passed | Return `InboundDelivery(payload, messages)`. `payload` (a JSON-serializable dict) is stored against every message; `messages` is one `InboundRef(message_id, conversation_key)` each. No messages means "acknowledge, persist nothing" (a status callback, a malformed body). Must not raise on attacker-shaped input. |
| `extract_turn` | In the deferred worker, once per stored message | Given the stored `payload` and the message's own id, return `InboundTurn(message_id, sender, text)`, or `None` to complete it without a turn (a non-text message, say). |
| `send_text` | In the deferred worker | Send `body` to `to` (the sender, normalized for this channel by the deployment's participant directory). Raise on failure: the worker retries a transient one and, when the message fails for good, sends the user a fallback reply once ([retry policy](deployment.md#retrying-failed-messages-and-the-failure-reply)). Delivery is **at least once** -- an ambiguous send can be repeated. |

Rules the platform relies on:

- `message_id` is the channel's own id for the message. The inbox dedups on it,
  scoped to the channel (stored as `<channel>:<id>`), so a provider that
  redelivers the same id is accepted once. It must be stable across
  redeliveries.
- `conversation_key` is what must be processed in arrival order -- normally
  the sender. It must never be empty; a message with no usable sender needs a
  key of its own (WhatsApp uses `unresolved:<id>`) so it cannot block anyone.
- Neither inbound method may touch the database: acceptance is the
  platform's.

An adapter whose provider verifies the webhook URL with a GET (Meta does) also
implements `SubscriptionHandshake.verify_subscription(params) -> str`; for any
other channel, `GET /webhook/{name}` answers 405.

WhatsApp's handshake compares `hub.verify_token` with `WHATSAPP_VERIFY_TOKEN` in
constant time, over UTF-8 bytes (so a non-ASCII token is a plain 403, never a
crash). The setting has no usable default: with the WhatsApp channel enabled
(`WHATSAPP_RUNTIME_ID` set) an empty `WHATSAPP_VERIFY_TOKEN` stops the boot,
because an empty configured token would match a request that sends an empty or
missing `hub.verify_token`. The adapter also refuses an empty configured token
by itself, whatever the boot check let through (`ALLOW_INSECURE=true` skips
only the boot check, for local development).

## Registering a channel

```python
from agents_system.channels.port import Channel
from agents_system.main import create_app

app = create_app(
    registry_factory=my_registry_factory,
    agents={"support": "support-agent"},
    grants={"support": ["read:catalog"]},
    channels={"telegram": Channel(adapter=TelegramChannel(...), runtime_id="support")},
)
```

- The channel is served at `POST /webhook/telegram` (and `GET` if it has a
  handshake). `/webhook` with no name stays the built-in WhatsApp channel;
  `/webhook/whatsapp` reaches it by name.
- A name is 1-32 lowercase letters, digits, `_` or `-`, starting with a letter.
  `whatsapp` is reserved. It is stored in `webhook_inbox.channel`, so treat
  it as permanent once messages have been accepted under it.
- `runtime_id` names the registered runtime that answers the channel. Boot
  fails unless it is registered, resolves `untrusted_input: true` (ADR-002
  C.13: a webhook channel delivers untrusted external input) and its execution
  budget leaves 60 seconds of headroom inside the outbox lease -- the same
  checks `WHATSAPP_RUNTIME_ID` gets.
- One deferred worker serves every channel. The turn's `thread_id` (and the
  recorder's thread) is the normalized sender scoped to the channel
  (`telegram:+54911...`), so two channels never share a conversation. It
  joins the shared checkpointer whenever the process builds one
  (`WHATSAPP_CHECKPOINTER_ENABLED`, which despite its name is the
  process-wide switch).
- Registering any channel requires a channel-aware participant directory
  (next section), or boot fails.

## Sending without an inbound turn

`send_text` only answers the turn in progress. A channel that can also start a conversation implements the optional `ProactiveChannel.send_proactive(to, body)`, raising `ProviderRateLimited(retry_after_s)` on a provider `429` and `ProviderRejected` on a definitive refusal. The platform side (recipient directory, `send:proactive`, durable at-most-once queue, rate limits, audit) is in [Proactive delivery](proactive.md).

## Participant identity is per channel (security requirement)

A sender address only means something inside the channel that authenticated
it. The same digits can be a WhatsApp customer's phone number and, on another
channel, a chat id or a value the sender controls. If one directory resolved
every channel's senders without knowing the channel, anyone able to reach a
second channel could send an existing customer's number and have the turn run
and be recorded under that customer's participant id.

So the directory is always told the channel:

```python
class ChannelParticipantDirectory(Protocol):
    def normalize_channel_address(self, raw: str, *, channel: str) -> str: ...
    async def resolve_channel_address(
        self, session: Any, address: str, *, channel: str
    ) -> Participant | None: ...
```

- The implementation decides, explicitly and per channel, which identity an
  address belongs to. Keep channels in separate identity spaces unless the
  deployment has its own proof that an address on one channel is the same
  person on another. Otherwise the contract is `ParticipantDirectory`'s:
  `ValueError` from normalization drops the delivery, and resolution owns its
  session's transaction.
- A `Participant.id` must be unique across every channel the directory serves. Per-user data is keyed `participant:<id>` ([ADR-011](../architecture/adr-011-end-user-scope.md)), so two channels share a subject only when the directory returns the same id for them. A directory that reuses ids across channels must namespace them before the deployment enables any per-user feature.
- The worker binds the participant it resolves as the turn's subject and `channel:<name>` as its actor. See [End-user scope](end-user-scope.md).
- A plain, channel-blind `ParticipantDirectory` keeps working for a
  WhatsApp-only deployment: WhatsApp is the address space it was written for.
- A worker bound to **any** other channel refuses a channel-blind directory.
  The lifespan builds that worker, so `create_app(channels=...)` with a
  channel-blind `participant_directory` fails at boot, naming the channels.

## Configuration belongs to the adapter

A channel's settings -- its token, its secret, its HTTP client -- are the
adapter's constructor arguments (or its own `pydantic-settings` class with
its own environment prefix). Nothing is added to the platform's flat
`Settings`, and `config.py` is never edited to add a channel.

The built-in WhatsApp channel follows the same rule in code:
`WhatsAppChannelSettings.from_settings(settings)` projects the WhatsApp slice
(`META_WEBHOOK_SECRET`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_GRAPH_API_URL`, `WHATSAPP_RUNTIME_ID`,
`WHATSAPP_CHECKPOINTER_ENABLED`) and the adapter reads only that view. The
environment variables keep their names: splitting them into a nested
settings model would rename every deployed variable for no behavioural gain,
so the "settings split" the original issue depended on is done as this view
rather than as a configuration break. Two leftovers are noted, not changed:
`META_PHONE_NUMBER_ID` and `META_ACCESS_TOKEN` are declared but read by no
code, and `webhook_max_body_bytes` / `webhook_worker_*` are, correctly,
channel-agnostic.

## Limits

- **Text only.** `InboundTurn` carries text; media is out of scope, as it is
  for WhatsApp.
- **One participant directory.** The deployment passes a single directory,
  and it serves every channel. It is told each address's channel (see above),
  so a channel whose addresses are not phone numbers is handled by that
  directory's per-channel logic.
- **Not for request/response APIs.** The OpenAI-compatible adapter
  (`/v1/chat/completions`, Open WebUI) answers in the same HTTP response; it
  has no inbound webhook, no durable handoff and no outbound send, so it is
  not a channel in this sense and stays where it is.

## Proof

`tests/test_channel_port.py` defines a fake channel only in the test file,
registers it through `create_app(channels=...)`, and drives one full turn
through the real route, the real `accept_inbound_message`, the real deferred
worker and a real harness-built `AgentRuntime` (only the outbox SQL and the
model provider are in-memory). The same file proves the directory is told
each sender's channel, and that a channel-blind directory is refused for a
second channel at worker construction and at boot. The unchanged
`tests/test_webhook.py` and `tests/test_webhook_worker.py` pin WhatsApp's
behaviour, channel-blind directory included.

> Spanish mirror: `docs/platform_es/channel.md` (keep both current together).
