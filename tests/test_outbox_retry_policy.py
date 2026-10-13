"""#290 -- which failures the outbox retries, and the per-runtime policy."""

from __future__ import annotations

from typing import Any

import pytest

from agents_system.services.retry_policy import (
    DEFAULT_FALLBACK_REPLY,
    DEFAULT_MAX_OUTBOX_ATTEMPTS,
    MAX_FALLBACK_REPLY_CHARS,
    OutboxRetryPolicy,
    is_transient_failure,
)


def _named(name: str, module: str, *bases: type, **attrs: Any) -> BaseException:
    """An exception of a class called *name* that claims to live in *module*,
    standing in for a provider, HTTP or database library's class without
    importing that library."""
    cls = type(name, bases or (Exception,), {"__module__": module, **attrs})
    return cls("boom")  # type: ignore[no-any-return]


class TestDefaultClassifier:
    @pytest.mark.parametrize(
        "exc",
        [
            TimeoutError("slow"),
            TimeoutError(),
            ConnectionError("reset"),
            ConnectionResetError(),
            BrokenPipeError(),
            _named("ReadTimeout", "httpx"),
            _named("ConnectError", "httpx"),
            _named("RemoteProtocolError", "httpcore"),
            _named("APITimeoutError", "openai"),
            _named("APIConnectionError", "anthropic"),
            _named("RateLimitError", "groq", status_code=429),
            _named("InternalServerError", "openai", status_code=500),
            _named("APIStatusError", "anthropic", status_code=529),
            _named("APIStatusError", "openai", status_code=408),
            _named("OperationalError", "sqlalchemy.exc"),
            _named("InterfaceError", "sqlalchemy.exc"),
            _named("TimeoutError", "sqlalchemy.exc"),
            _named("ConnectionDoesNotExistError", "asyncpg.exceptions"),
            _named("CannotConnectNowError", "asyncpg.exceptions"),
            _named("ConnectionError", "redis.exceptions"),
            _named("TimeoutError", "redis.exceptions"),
        ],
    )
    def test_infrastructure_and_provider_overload_are_transient(
        self, exc: BaseException
    ) -> None:
        assert is_transient_failure(exc) is True

    @pytest.mark.parametrize(
        "exc",
        [
            ValueError("bad tool input"),
            KeyError("missing"),
            TypeError("nope"),
            RuntimeError("bug"),
            AssertionError(),
            PermissionError("security abort"),
            FileNotFoundError("x"),
            _named("APIStatusError", "openai", status_code=400),
            _named("AuthenticationError", "anthropic", status_code=401),
            _named("NotFoundError", "openai", status_code=404),
            _named("IntegrityError", "sqlalchemy.exc"),
            _named("ProgrammingError", "sqlalchemy.exc"),
            _named("UniqueViolationError", "asyncpg.exceptions"),
        ],
    )
    def test_everything_else_is_permanent(self, exc: BaseException) -> None:
        assert is_transient_failure(exc) is False

    def test_a_transient_cause_makes_a_wrapper_transient(self) -> None:
        try:
            try:
                raise ConnectionError("reset")
            except ConnectionError as cause:
                raise RuntimeError("model call failed") from cause
        except RuntimeError as wrapped:
            assert is_transient_failure(wrapped) is True

    def test_an_implicit_context_does_not_make_a_bug_transient(self) -> None:
        try:
            try:
                raise ConnectionError("reset")
            except ConnectionError:
                raise ValueError("a bug while handling it")
        except ValueError as bug:
            assert is_transient_failure(bug) is False

    def test_a_cyclic_cause_chain_terminates(self) -> None:
        first, second = ValueError("a"), ValueError("b")
        first.__cause__ = second
        second.__cause__ = first
        assert is_transient_failure(first) is False


class TestPolicy:
    def test_defaults_keep_todays_behavior(self) -> None:
        policy = OutboxRetryPolicy()
        assert policy.max_attempts == DEFAULT_MAX_OUTBOX_ATTEMPTS == 5
        assert policy.fallback_reply == DEFAULT_FALLBACK_REPLY
        assert policy.is_transient is is_transient_failure

    def test_a_predicate_replaces_the_classifier(self) -> None:
        policy = OutboxRetryPolicy(is_transient=lambda exc: isinstance(exc, KeyError))
        assert policy.is_transient(KeyError("x")) is True
        assert policy.is_transient(TimeoutError()) is False

    def test_exception_types_build_a_policy(self) -> None:
        policy = OutboxRetryPolicy.retrying(LookupError, max_attempts=2)
        assert policy.max_attempts == 2
        assert policy.is_transient(KeyError("x")) is True
        assert policy.is_transient(TimeoutError()) is False

    def test_exception_types_can_extend_the_default_classifier(self) -> None:
        policy = OutboxRetryPolicy.retrying(LookupError, include_default=True)
        assert policy.is_transient(KeyError("x")) is True
        assert policy.is_transient(TimeoutError()) is True
        assert policy.is_transient(ValueError("x")) is False

    @pytest.mark.parametrize("attempts", [0, -1, 1.5, True, "5"])
    def test_max_attempts_must_be_a_positive_int(self, attempts: Any) -> None:
        with pytest.raises(ValueError, match="max_attempts"):
            OutboxRetryPolicy(max_attempts=attempts)

    @pytest.mark.parametrize("reply", ["", "   ", None, 3])
    def test_the_fallback_reply_must_be_text(self, reply: Any) -> None:
        with pytest.raises(ValueError, match="fallback reply"):
            OutboxRetryPolicy(fallback_reply=reply)

    def test_the_fallback_reply_is_bounded(self) -> None:
        with pytest.raises(ValueError, match="fallback reply"):
            OutboxRetryPolicy(fallback_reply="x" * (MAX_FALLBACK_REPLY_CHARS + 1))

    def test_a_non_callable_classifier_is_refused(self) -> None:
        with pytest.raises(ValueError, match="is_transient"):
            OutboxRetryPolicy(is_transient="nope")  # type: ignore[arg-type]
