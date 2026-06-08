import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx


@pytest.fixture
def mock_ha_response():
    return [
        {"entity_id": "light.kitchen", "state": "on"},
        {"entity_id": "sensor.temp", "state": "unavailable"},
    ]


@pytest.mark.asyncio
async def test_homeassistant_fetch(mock_ha_response):
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = mock_ha_response
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.homeassistant import fetch
        data = await fetch()
        assert len(data.entities) == 2
        assert "sensor.temp" in data.alerts


@pytest.mark.asyncio
async def test_homeassistant_health_check_ok():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.homeassistant import health_check
        assert await health_check() is True


@pytest.mark.asyncio
async def test_homeassistant_health_check_fail():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("timeout"))
        mock_client_cls.return_value = mock_client

        from backend.integrations.homeassistant import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_weather_fetch():
    current = {"weather": [{"main": "Clear"}], "main": {"temp": 294.15, "feels_like": 293.0}, "wind": {"speed": 5.0}}
    forecast = {"list": [
        {"main": {"temp": 298.0}, "pop": 0.1},
        {"main": {"temp": 290.0}, "pop": 0.4},
    ]}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_current_resp = MagicMock(status_code=200)
        mock_current_resp.raise_for_status = MagicMock()
        mock_current_resp.json.return_value = current
        mock_forecast_resp = MagicMock(status_code=200)
        mock_forecast_resp.json.return_value = forecast

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(
            side_effect=[mock_current_resp, mock_forecast_resp]
        )
        mock_client_cls.return_value = mock_client

        from backend.integrations.weather import fetch, _k_to_f
        data = await fetch()
        assert data.condition == "Clear"
        assert abs(data.temp_f - _k_to_f(294.15)) < 0.5
        assert data.precip_chance_pct == 40


@pytest.mark.asyncio
async def test_adguard_fetch():
    stats = {"num_dns_queries": 1000, "num_blocked_filtering": 234, "top_blocked_domains": {"ads.example.com": 50}, "top_clients": {}}
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_stats = MagicMock(status_code=200)
        mock_stats.raise_for_status = MagicMock()
        mock_stats.json.return_value = stats
        mock_status = MagicMock(status_code=200)
        mock_status.json.return_value = {"protection_enabled": True}

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[mock_stats, mock_status])
        mock_client_cls.return_value = mock_client

        from backend.integrations.adguard import fetch
        data = await fetch()
        assert data.queries_today == 1000
        assert data.blocked_today == 234
        assert data.blocked_pct == 23.4
        assert data.filtering_enabled is True


@pytest.mark.asyncio
async def test_channels_fetch():
    dvr_data = {"storage_used": 1024**3 * 500, "storage_total": 1024**3 * 2000}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_dvr = MagicMock(status_code=200)
        mock_dvr.json.return_value = dvr_data
        mock_jobs = MagicMock(status_code=200)
        mock_jobs.json.return_value = []
        mock_upcoming = MagicMock(status_code=200)
        mock_upcoming.json.return_value = []

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[mock_dvr, mock_jobs, mock_upcoming])
        mock_client_cls.return_value = mock_client

        from backend.integrations.channels_dvr import fetch
        data = await fetch()
        assert data.storage_used_gb == 500.0
        assert data.storage_total_gb == 2000.0


@pytest.mark.asyncio
async def test_hermes_health_alive():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {"last_seen": "2024-01-01T00:00:00", "pending_actions": 0}
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import health_check
        assert await health_check() is True


@pytest.mark.asyncio
async def test_hermes_health_dead():
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import health_check
        assert await health_check() is False
