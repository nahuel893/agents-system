"""Tests for message deduplication service (Redis SET NX)."""

from __future__ import annotations

from unittest.mock import AsyncMock

from badie.services.dedup import is_duplicate


# ---------------------------------------------------------------------------
# Phase 1 — is_duplicate unit tests (tasks 1.1–1.3)
# ---------------------------------------------------------------------------


async def test_is_duplicate_new_message() -> None:
    """New message_id → Redis SET returns True → is_duplicate returns False."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=True)  # key was created

    result = await is_duplicate(mock_redis, "wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf")

    assert result is False
    mock_redis.set.assert_called_once_with(
        "dedup:wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf", "1", nx=True, ex=300
    )


async def test_is_duplicate_duplicate_message() -> None:
    """Already-seen message_id → Redis SET returns None → is_duplicate returns True."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(return_value=None)  # key already existed

    result = await is_duplicate(mock_redis, "wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf")

    assert result is True


async def test_is_duplicate_redis_error() -> None:
    """Redis unavailable → is_duplicate returns False (fail-open)."""
    mock_redis = AsyncMock()
    mock_redis.set = AsyncMock(side_effect=ConnectionError("Redis down"))

    result = await is_duplicate(mock_redis, "wamid.ABGGFlA5FpafAgo6tHcNmNjXmuSf")

    assert result is False  # fail-open: process the message
