"""Redis async connection manager — shared pool singleton."""

from __future__ import annotations

from agents_system.extras import raise_missing_extra

try:
    import redis.asyncio as redis
except ImportError as exc:
    raise_missing_extra("server", "The Redis connection pool", exc)

_pool: redis.ConnectionPool | None = None


def get_redis_pool(url: str) -> redis.ConnectionPool:
    """Create or return existing connection pool."""
    global _pool
    if _pool is None:
        _pool = redis.ConnectionPool.from_url(url, decode_responses=True)
    return _pool


def get_redis_client(url: str) -> redis.Redis:
    """Get an async Redis client using the shared pool."""
    pool = get_redis_pool(url)
    return redis.Redis(connection_pool=pool)


async def close_redis_pool() -> None:
    """Close the connection pool. Call on app shutdown."""
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None
