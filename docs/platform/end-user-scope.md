# End-user scope: turn subject and actor

*Coming in the next release.* Decided in [ADR-011](../architecture/adr-011-end-user-scope.md); this page covers part 1 (identity bound per turn) and part 2 (audit and logs).

A deployed agent serves many end users through one cached runtime. Each turn carries two values the entry point binds and the model never sees:

| Value | Meaning | Shape |
|---|---|---|
| `TurnSubject` | The end user the turn is for | `key` (`participant:<id>`), plus `channel` and `address` for delivery only |
| `TurnActor` | Who started the turn | `kind` (`channel`, `api`, `operator`, `scheduler`, `library`) and `id`; its label is `<kind>:<id>` |

```python
from agents_system import TurnActor, TurnSubject

subject = TurnSubject.for_participant(42, channel="whatsapp", address="+5491100000000")
await runtime.run_turn(
    messages,
    session_id="s1",
    subject=subject,
    actor=TurnActor(kind="library", id="job"),
)
```

Both are keyword-only and default to `None`; a turn without them behaves as before. The key uses the participant id, not the address, because an address can be reassigned to another person.

## Who binds what

| Turn | Subject | Actor |
|---|---|---|
| Inbound channel turn | The participant the worker resolves | `channel:<channel>` |
| API turn | Only from a `SubjectResolver`; otherwise none | `api:<runtime id>` |
| Library call | Whatever the caller passes | Whatever the caller passes |

The OpenAI-compatible adapter authenticates one shared key, so it knows no end user. A deployment that does supplies a resolver:

```python
class MyResolver:
    async def resolve(self, request):  # -> Participant | None
        ...


app = create_app(registry_factory=..., subject_resolver=MyResolver())
```

`None` means the request has no end user and the turn runs with no subject. An inactive participant is refused with 403 and no turn runs. The request's `user` field and message text are never a subject.

## What it reaches

- **Audit:** every event of the turn fills `actor` (`<kind>:<id>`) and the new nullable `subject` column (migration `008`) with the subject key. The address never goes in a column. See [Audit](audit.md).
- **Turn logs:** `runtime.turn_usage` and every log line of the turn carry `subject` (the key) and `actor`, never the address.
- **Participant ids:** a `Participant.id` must be unique across every channel the directory serves. See [Channel](channel.md).

Tools do not receive the subject yet: the scoped handle and per-user stores come with ADR-011 part 3.
