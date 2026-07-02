import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _get_response(data, status_code: int = 200):
    """Mock the single GET response fetch/health_check issue.
    `data` is the value of the JSON top-level 'data' field."""
    resp = MagicMock(status_code=status_code)
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {"data": data}
    return resp


def _get_client(resp):
    client = AsyncMock()
    client.__aenter__.return_value.get = AsyncMock(return_value=resp)
    return client


_GIB = 1024 ** 3


@pytest.fixture(autouse=True)
def _token_configured():
    """Every test drives fetch/health_check with a configured token unless it
    overrides this. fetch() short-circuits to a raise when the token is empty."""
    with patch("backend.config.get_settings") as mock_gs:
        settings = MagicMock()
        settings.proxmox_host = "https://192.168.1.60:8006"
        settings.proxmox_token = "PVEAPIToken=nexus@pve!ro=deadbeef"
        mock_gs.return_value = settings
        yield settings


@pytest.mark.asyncio
async def test_fetch_parses_cluster_resources():
    rows = [
        {"type": "node", "node": "pve", "status": "online", "cpu": 0.25,
         "mem": 8 * _GIB, "maxmem": 32 * _GIB},
        {"type": "qemu", "vmid": 101, "name": "win11", "status": "running"},
        {"type": "lxc", "vmid": 200, "name": "hermes", "status": "running"},
        {"type": "storage", "disk": 100 * _GIB, "maxdisk": 500 * _GIB},
        {"type": "storage", "disk": 50 * _GIB, "maxdisk": 500 * _GIB},
    ]
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _get_client(_get_response(rows))
        from backend.integrations.proxmox import fetch
        result = await fetch()

    assert result.node == "pve"
    assert result.node_status == "online"
    assert result.cpu_pct == 25.0
    assert result.mem_used_gb == 8.0
    assert result.mem_total_gb == 32.0
    assert len(result.vms) == 2
    vmids = {v["vmid"]: v for v in result.vms}
    assert vmids[101]["type"] == "qemu"
    assert vmids[101]["name"] == "win11"
    assert vmids[200]["type"] == "lxc"
    assert vmids[200]["status"] == "running"
    # storage summed across both storage rows
    assert result.storage_used_gb == 150.0
    assert result.storage_total_gb == 1000.0


@pytest.mark.asyncio
async def test_fetch_http_error_raises():
    """A non-2xx response (raise_for_status raises) must RAISE — not return
    zero-filled defaults that look like a dead node to the briefing/trends."""
    resp = _get_response(None, status_code=500)
    resp.raise_for_status.side_effect = Exception("HTTP 500")
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _get_client(resp)
        from backend.integrations.proxmox import fetch
        with pytest.raises(RuntimeError):
            await fetch()


@pytest.mark.asyncio
async def test_fetch_connection_error_raises():
    """A connection failure (get raises) must propagate as unavailable, not zeros."""
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = client
        from backend.integrations.proxmox import fetch
        with pytest.raises(RuntimeError):
            await fetch()


@pytest.mark.asyncio
async def test_fetch_missing_data_raises():
    """An empty/missing data array must RAISE — never zero-default."""
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _get_client(_get_response([]))
        from backend.integrations.proxmox import fetch
        with pytest.raises(RuntimeError):
            await fetch()


@pytest.mark.asyncio
async def test_health_check_ok():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _get_client(_get_response({"version": "8.1"}))
        from backend.integrations.proxmox import health_check
        assert await health_check() is True


@pytest.mark.asyncio
async def test_health_check_fail():
    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.__aenter__.return_value.get = AsyncMock(side_effect=Exception("connection refused"))
        mock_cls.return_value = client
        from backend.integrations.proxmox import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_health_check_non_200():
    with patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _get_client(_get_response({"version": "8.1"}, status_code=401))
        from backend.integrations.proxmox import health_check
        assert await health_check() is False


@pytest.mark.asyncio
async def test_health_check_unconfigured_false():
    """No token configured -> OFFLINE (False), never a crash, and no HTTP call."""
    with patch("backend.config.get_settings") as mock_gs:
        settings = MagicMock()
        settings.proxmox_host = "https://192.168.1.60:8006"
        settings.proxmox_token = ""
        mock_gs.return_value = settings
        with patch("httpx.AsyncClient") as mock_cls:
            from backend.integrations.proxmox import health_check
            assert await health_check() is False
            mock_cls.assert_not_called()


def test_proxmox_in_sources_registry():
    """proxmox must be registered in the /api/sources/status registry."""
    import inspect
    from backend.api import sources
    src = inspect.getsource(sources.sources_status)
    assert '"proxmox": proxmox' in src


def test_proxmox_data_defaults():
    from backend.integrations.proxmox import ProxmoxData
    data = ProxmoxData()
    assert data.node_status == "unknown"
    assert data.vms == []
    assert data.storage_total_gb == 0.0
    assert data.mem_total_gb == 0.0
