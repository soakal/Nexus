from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_run_speedtest_offline_returns_not_online():
    # The ping probe failing => offline => heavy transfers skipped, online False.
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("network unreachable"))
        mock_cls.return_value = client

        from backend.integrations.speedtest import run_speedtest
        result = await run_speedtest()

    assert result["online"] is False
    assert result["download_mbps"] == 0.0
    assert result["upload_mbps"] == 0.0


@pytest.mark.asyncio
async def test_record_speedtest_skips_db_write_when_offline():
    from backend.scheduler import _record_speedtest
    offline = {"download_mbps": 0.0, "upload_mbps": 0.0, "ping_ms": 0.0, "online": False}
    with patch("backend.integrations.speedtest.run_speedtest", new=AsyncMock(return_value=offline)), \
         patch("sqlmodel.Session") as mock_session:
        await _record_speedtest()
        # No session/commit should happen when offline.
        mock_session.assert_not_called()


@pytest.mark.asyncio
async def test_record_speedtest_writes_when_online():
    from backend.scheduler import _record_speedtest
    online = {"download_mbps": 300.0, "upload_mbps": 20.0, "ping_ms": 12.0, "online": True}
    session = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=session)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("backend.integrations.speedtest.run_speedtest", new=AsyncMock(return_value=online)), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session", return_value=cm):
        await _record_speedtest()
        session.add.assert_called_once()
        session.commit.assert_called_once()
