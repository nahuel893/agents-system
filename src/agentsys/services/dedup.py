"""Message deduplication via Redis SET NX with TTL."""

from __future__ import annotations

import redis.asyncio as redis
import structlog

logger = structlog.get_logger()

DEDUP_TTL_SECONDS = 300  # 5 minutes — matches Meta's webhook retry window


async def is_duplicate(redis_client: redis.Redis, message_id: str) -> bool:
    """Check if message_id was already processed.

    Uses Redis SET NX EX for atomic check-and-set with TTL.

    Returns:
        True if duplicate (key existed), False if new (key created).
        Fails open on Redis errors (returns False + logs warning).
    """
    try:
        result = await redis_client.set(
            f"dedup:{message_id}", "1", nx=True, ex=DEDUP_TTL_SECONDS
        )
        return result is None  # None = key already existed = duplicate
    except Exception:
        logger.warning("dedup.redis_error", message_id=message_id)
        return False  # fail-open: process the message
