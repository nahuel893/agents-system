"""Message deduplication via Redis SET NX with TTL.

W2b2 decision: superseded, on the live route, by the durable inbox's
``webhook_inbox.meta_message_id`` unique constraint (migration ``002``,
enforced transactionally by ``services.outbox.accept_inbound_message``). That
constraint is what ``POST /webhook`` now relies on for duplicate-delivery
detection, and it does not have this module's two weaknesses: it never fails
open on a Redis outage (a persistence error there returns 503 -- see
``integration/webhook.py`` -- rather than silently reprocessing), and it is
not bounded by a TTL a slow retry can outlive. ``is_duplicate`` is no longer
called from the route (nothing in the platform calls it anymore), so this is
not two mechanisms disagreeing on the same request -- there is exactly one
live one. The function and ``DEDUP_TTL_SECONDS`` stay only because
``main.py`` still asserts an unrelated static invariant against
``DEDUP_TTL_SECONDS`` (bounding the platform's default turn-execution timeout
against it); untangling that invariant, now that no turn ever runs inside the
webhook request it was written to guard, is intentionally left out of W2b2's
scope -- see the task doc's "unresolved" note.
"""

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
