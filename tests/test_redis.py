"""Tests for the Redis connection manager (no running Redis required)."""

import agentsys.services.redis as redis_mod


def test_get_redis_pool_returns_pool() -> None:
    redis_mod._pool = None
    pool = redis_mod.get_redis_pool("redis://localhost:6379/0")
    assert pool is not None
    redis_mod._pool = None


def test_get_redis_pool_is_singleton() -> None:
    redis_mod._pool = None
    p1 = redis_mod.get_redis_pool("redis://localhost:6379/0")
    p2 = redis_mod.get_redis_pool("redis://localhost:6379/0")
    assert p1 is p2
    redis_mod._pool = None


def test_get_redis_client_returns_client() -> None:
    redis_mod._pool = None
    client = redis_mod.get_redis_client("redis://localhost:6379/0")
    assert client is not None
    redis_mod._pool = None
