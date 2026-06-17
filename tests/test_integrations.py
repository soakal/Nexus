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
    dvr_data = {"disk": {"total": 1024**3 * 2000, "free": 1024**3 * 1500, "used": 1024**3 * 500}}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_dvr = MagicMock(status_code=200)
        mock_dvr.json.return_value = dvr_data
        mock_jobs = MagicMock(status_code=200)
        mock_jobs.json.return_value = []

        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=[mock_dvr, mock_jobs])
        mock_client_cls.return_value = mock_client

        from backend.integrations.channels_dvr import fetch
        data = await fetch()
        assert data.storage_used_gb == 500.0
        assert data.storage_total_gb == 2000.0


@pytest.mark.asyncio
async def test_channels_fetch_dvr_non200_raises():
    """A non-200 /dvr disk-stats read must RAISE (treated as unavailable), NOT report
    0.0 GB storage that looks like data loss to the briefing/trends/proposer."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_dvr = MagicMock(status_code=503)
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(return_value=mock_dvr)
        mock_client_cls.return_value = mock_client

        from backend.integrations.channels_dvr import fetch
        with pytest.raises(Exception):
            await fetch()


@pytest.mark.asyncio
async def test_channels_fetch_connection_error_raises():
    """A connection failure on the disk-stats read propagates as unavailable."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value = mock_client

        from backend.integrations.channels_dvr import fetch
        with pytest.raises(Exception):
            await fetch()


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


def test_hermes_ok_from_action_json():
    """The #2 structured-action signal: explicit 'ok' wins; otherwise the
    'error'-prefix heuristic; absent/blank degrades to True (back-compat)."""
    from backend.integrations.hermes import _ok_from_action_json
    assert _ok_from_action_json({"ok": True, "response": "ok: start sent to 101"}) is True
    assert _ok_from_action_json({"ok": False, "response": "error: Proxmox 500"}) is False
    # Pre-#2 Hermes (no 'ok' field) — fall back to the response prefix.
    assert _ok_from_action_json({"response": "error: invalid action"}) is False
    assert _ok_from_action_json({"response": "VMs/LXCs:\n..."}) is True
    # Absent/blank body degrades to True (HTTP 2xx already gated the call).
    assert _ok_from_action_json({}) is True
    assert _ok_from_action_json({"response": ""}) is True


@pytest.mark.asyncio
async def test_hermes_relay_action_failure_signal():
    """relay_action surfaces a Hermes-side {'ok': false} as ok=False (not a
    success string), so the broker can record the action FAILED."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_resp = MagicMock(status_code=200)
        mock_resp.json.return_value = {
            "ok": False,
            "response": "error: Proxmox returned 500",
            "intent": "vm_action",
        }
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import relay_action
        result = await relay_action("start 101")
        assert result["ok"] is False
        assert "500" in result["response"]
        assert result["intent"] == "vm_action"


@pytest.mark.asyncio
async def test_hermes_relay_action_unreachable():
    """A transport failure yields ok=False with the detail in 'response'."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client_cls.return_value = mock_client

        from backend.integrations.hermes import relay_action
        result = await relay_action("start 101")
        assert result["ok"] is False
        assert "not reachable" in result["response"].lower()
