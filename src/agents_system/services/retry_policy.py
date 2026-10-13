"""Which failed outbox work is retried, and what the user is told when it is not (#290).

Each retry re-runs the whole turn (model calls, SQL, code execution), so only
a failure that can go away on its own is worth it. A failure that will repeat
with the same input -- a bug, a bad tool input, a security abort -- is
permanent: one attempt, then the end user is told once.

Nothing here imports a provider, HTTP or database package. Those libraries are
optional extras, so a failure is recognised by its class name and module, by
an HTTP ``status_code`` it carries, or by being a Python built-in network or
timeout error. This module has no dependency on the ``postgres`` extra, so a
library user can build a policy without it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

#: The most times the outbox runs one work item before it gives up (as before).
DEFAULT_MAX_OUTBOX_ATTEMPTS = 5

#: Sent to the end user when their message could not be answered. Plain text.
DEFAULT_FALLBACK_REPLY = (
    "Sorry, something went wrong while handling your message. Please try again."
)
MAX_FALLBACK_REPLY_CHARS = 1000

# HTTP statuses a retry can fix: the request timed out, was rate limited, or
# the server failed. Every other 4xx repeats with the same input.
_TRANSIENT_STATUSES = frozenset({408, 429})

# Only the library part of a module path decides; the class names are the
# ones those libraries document for "the connection or the server, not you".
_TRANSIENT_BY_LIBRARY: dict[str, frozenset[str]] = {
    "httpx": frozenset(
        {
            "TransportError",
            "TimeoutException",
            "ConnectTimeout",
            "ReadTimeout",
            "WriteTimeout",
            "PoolTimeout",
            "NetworkError",
            "ConnectError",
            "ReadError",
            "WriteError",
            "CloseError",
            "RemoteProtocolError",
            "ProxyError",
        }
    ),
    "openai": frozenset(
        {
            "APITimeoutError",
            "APIConnectionError",
            "RateLimitError",
            "InternalServerError",
        }
    ),
    "anthropic": frozenset(
        {
            "APITimeoutError",
            "APIConnectionError",
            "RateLimitError",
            "InternalServerError",
        }
    ),
    "groq": frozenset(
        {
            "APITimeoutError",
            "APIConnectionError",
            "RateLimitError",
            "InternalServerError",
        }
    ),
    "sqlalchemy": frozenset(
        {"OperationalError", "InterfaceError", "DisconnectionError", "TimeoutError"}
    ),
    "asyncpg": frozenset(
        {
            "PostgresConnectionError",
            "ConnectionDoesNotExistError",
            "ConnectionFailureError",
            "CannotConnectNowError",
            "TooManyConnectionsError",
            "InterfaceError",
        }
    ),
    "psycopg": frozenset({"OperationalError", "InterfaceError"}),
    "psycopg2": frozenset({"OperationalError", "InterfaceError"}),
    "redis": frozenset(
        {"ConnectionError", "TimeoutError", "BusyLoadingError", "ClusterDownError"}
    ),
}

_TRANSIENT_BY_LIBRARY["httpcore"] = _TRANSIENT_BY_LIBRARY["httpx"]

# A cause chain is short; this bounds a pathological or cyclic one.
_MAX_CAUSE_DEPTH = 8


def _transient_by_name(exc: BaseException) -> bool:
    for cls in type(exc).__mro__:
        library = cls.__module__.partition(".")[0]
        if cls.__name__ in _TRANSIENT_BY_LIBRARY.get(library, ()):
            return True
    return False


def _status_code(exc: BaseException) -> int | None:
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _is_transient_one(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError | ConnectionError):
        return True
    status = _status_code(exc)
    if status is not None:
        return status in _TRANSIENT_STATUSES or status >= 500
    return _transient_by_name(exc)


def is_transient_failure(exc: BaseException) -> bool:
    """Whether retrying the same work could succeed.

    Transient: provider rate limits (429), 5xx and timeouts, network and
    connection errors, database connection errors, ``TimeoutError`` and
    ``ConnectionError``. An exception raised ``from`` a transient one counts
    too (a wrapper around a dropped connection); an implicit ``__context__``
    does not, since a bug raised while handling a network error is still a
    bug. Everything else is permanent.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            return False
        if _is_transient_one(current):
            return True
        seen.add(id(current))
        current = current.__cause__
    return False


def validate_fallback_reply(text: Any, *, who: str) -> str:
    """A fallback reply, checked: non-blank plain text, bounded. Sent as given."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(
            f"fallback reply for {who} must be non-empty plain text, got {text!r}"
        )
    if len(text) > MAX_FALLBACK_REPLY_CHARS:
        raise ValueError(
            f"fallback reply for {who} has {len(text)} characters; the most is "
            f"{MAX_FALLBACK_REPLY_CHARS}"
        )
    return text


@dataclass(frozen=True)
class OutboxRetryPolicy:
    """How one runtime's channel work is retried and what the user is told.

    ``max_attempts`` is the most times a work item runs (``1``: never retry).
    ``is_transient`` decides, from the exception, whether another attempt is
    worth it; the default is ``is_transient_failure``. ``fallback_reply`` is
    sent once to the end user when the work fails for good, as given and
    never templated with user or tool data.
    """

    max_attempts: int = DEFAULT_MAX_OUTBOX_ATTEMPTS
    is_transient: Callable[[BaseException], bool] = field(default=is_transient_failure)
    fallback_reply: str = DEFAULT_FALLBACK_REPLY

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValueError(
                f"max_attempts must be a positive integer, got {self.max_attempts!r}"
            )
        if not callable(self.is_transient):
            raise ValueError(  # noqa: TRY004 - validators raise ValueError
                "is_transient must be a callable taking the exception"
            )
        validate_fallback_reply(self.fallback_reply, who="an outbox retry policy")

    @classmethod
    def retrying(
        cls,
        *types: type[BaseException],
        include_default: bool = False,
        **fields: Any,
    ) -> OutboxRetryPolicy:
        """A policy that retries instances of *types*; with ``include_default``
        also what ``is_transient_failure`` retries."""

        def is_transient(exc: BaseException) -> bool:
            return isinstance(exc, types) or (
                include_default and is_transient_failure(exc)
            )

        return cls(is_transient=is_transient, **fields)


def build_retry_policies(
    *,
    policies: Mapping[str, OutboxRetryPolicy | Mapping[str, Any]] | None = None,
    max_attempts: Mapping[str, int] | None = None,
    fallback_replies: Mapping[str, str] | None = None,
) -> dict[str, OutboxRetryPolicy]:
    """``{runtime_id: policy}`` from ``create_app(outbox_retry=...)`` and the
    ``OUTBOX_MAX_ATTEMPTS`` / ``OUTBOX_FALLBACK_REPLY`` settings.

    A policy given for a runtime id is used as it is (the environment cannot
    carry a classifier, so it never merges into one); a runtime named only by
    the environment gets the defaults for whatever the environment leaves out.
    A runtime named by neither is absent: it keeps the defaults.
    """
    built: dict[str, OutboxRetryPolicy] = {}
    for runtime_id, given in (policies or {}).items():
        who = f"runtime {str(runtime_id)!r}"
        if isinstance(given, OutboxRetryPolicy):
            built[str(runtime_id)] = given
        elif isinstance(given, Mapping):
            try:
                built[str(runtime_id)] = OutboxRetryPolicy(**given)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"outbox retry policy for {who}: {exc}") from exc
        else:
            raise ValueError(  # noqa: TRY004 - validators raise ValueError
                f"outbox retry policy for {who} must be an OutboxRetryPolicy "
                f"or a mapping of its fields, got {type(given).__name__}"
            )
    for runtime_id in sorted(set(max_attempts or {}) | set(fallback_replies or {})):
        if str(runtime_id) in built:
            continue
        who = f"runtime {str(runtime_id)!r}"
        fields: dict[str, Any] = {}
        if max_attempts and runtime_id in max_attempts:
            fields["max_attempts"] = max_attempts[runtime_id]
        if fallback_replies and runtime_id in fallback_replies:
            fields["fallback_reply"] = fallback_replies[runtime_id]
        try:
            built[str(runtime_id)] = OutboxRetryPolicy(**fields)
        except ValueError as exc:
            raise ValueError(f"outbox retry policy for {who}: {exc}") from exc
    return built
