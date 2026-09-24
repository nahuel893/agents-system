"""Smoke test for the demo API entrypoint."""

from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from agents_system.demo import build_app


async def test_demo_app_health_is_ok_without_lifespan() -> None:
    """The demo app exposes /health without connecting to real services."""
    app = build_app(engine=MagicMock(), model=MagicMock())

    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.execute = AsyncMock(return_value=MagicMock())

    mock_engine = MagicMock()
    mock_engine.connect = MagicMock(return_value=mock_conn)
    mock_engine.dispose = AsyncMock()

    mock_redis = AsyncMock()
    mock_redis.ping = AsyncMock(return_value=True)

    app.state.engine = mock_engine

    with patch("agents_system.main.get_redis_client", return_value=mock_redis):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
