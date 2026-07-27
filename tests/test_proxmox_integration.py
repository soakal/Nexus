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


# ---------------------------------------------------------------------------
# fetch_updates / fetch_backups (Feature 2 — dashboard maintenance badges)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_updates_parses_count_and_names():
    from backend.integrations.proxmox import ProxmoxData
    pkgs = [{"Package": f"pkg{i}"} for i in range(20)]
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.return_value = ProxmoxData(node="pve")
        mock_cls.return_value = _get_client(_get_response(pkgs))
        from backend.integrations.proxmox import fetch_updates
        result = await fetch_updates()

    assert result["node"] == "pve"
    assert result["count"] == 20
    assert result["packages"] == [f"pkg{i}" for i in range(15)]  # first 15 only


@pytest.mark.asyncio
async def test_fetch_updates_empty_is_zero_not_a_raise():
    """An up-to-date node (empty apt data) is a real result, not a failure —
    differs from fetch()'s own empty-rows-raises contract."""
    from backend.integrations.proxmox import ProxmoxData
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.return_value = ProxmoxData(node="pve")
        mock_cls.return_value = _get_client(_get_response([]))
        from backend.integrations.proxmox import fetch_updates
        result = await fetch_updates()

    assert result == {"node": "pve", "count": 0, "packages": []}


@pytest.mark.asyncio
async def test_fetch_updates_http_error_raises():
    from backend.integrations.proxmox import ProxmoxData
    resp = _get_response(None, status_code=500)
    resp.raise_for_status.side_effect = Exception("HTTP 500")
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.return_value = ProxmoxData(node="pve")
        mock_cls.return_value = _get_client(resp)
        from backend.integrations.proxmox import fetch_updates
        with pytest.raises(RuntimeError):
            await fetch_updates()


@pytest.mark.asyncio
async def test_fetch_backups_picks_newest_ok():
    from backend.integrations.proxmox import ProxmoxData
    tasks = [
        {"type": "vzdump", "status": "OK", "starttime": 100, "endtime": 200},
        {"type": "vzdump", "status": "OK", "starttime": 300, "endtime": 400},  # newest
        {"type": "other", "status": "OK", "starttime": 500, "endtime": 600},  # non-vzdump, ignored
    ]
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.return_value = ProxmoxData(node="pve")
        mock_cls.return_value = _get_client(_get_response(tasks))
        from backend.integrations.proxmox import fetch_backups
        result = await fetch_backups()

    assert result == {"node": "pve", "status": "ok", "detail": "OK", "endtime": 400}


@pytest.mark.asyncio
async def test_fetch_backups_failed_status():
    from backend.integrations.proxmox import ProxmoxData
    tasks = [{"type": "vzdump", "status": "job errors", "starttime": 100, "endtime": 150}]
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.return_value = ProxmoxData(node="pve")
        mock_cls.return_value = _get_client(_get_response(tasks))
        from backend.integrations.proxmox import fetch_backups
        result = await fetch_backups()

    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_fetch_backups_running_status():
    from backend.integrations.proxmox import ProxmoxData
    tasks = [{"type": "vzdump", "status": None, "starttime": 100}]
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.return_value = ProxmoxData(node="pve")
        mock_cls.return_value = _get_client(_get_response(tasks))
        from backend.integrations.proxmox import fetch_backups
        result = await fetch_backups()

    assert result["status"] == "running"
    assert result["endtime"] == 100  # falls back to starttime when no endtime


@pytest.mark.asyncio
async def test_fetch_backups_no_tasks_is_none_status():
    from backend.integrations.proxmox import ProxmoxData
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.return_value = ProxmoxData(node="pve")
        mock_cls.return_value = _get_client(_get_response([]))
        from backend.integrations.proxmox import fetch_backups
        result = await fetch_backups()

    assert result == {"node": "pve", "status": "none"}


@pytest.mark.asyncio
async def test_fetch_backups_http_error_raises():
    from backend.integrations.proxmox import ProxmoxData
    resp = _get_response(None, status_code=500)
    resp.raise_for_status.side_effect = Exception("HTTP 500")
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.return_value = ProxmoxData(node="pve")
        mock_cls.return_value = _get_client(resp)
        from backend.integrations.proxmox import fetch_backups
        with pytest.raises(RuntimeError):
            await fetch_backups()


# ---------------------------------------------------------------------------
# Phase 7b — set_vm_power (native, not via Hermes)
# ---------------------------------------------------------------------------

def _post_client(resp):
    client = AsyncMock()
    client.__aenter__.return_value.post = AsyncMock(return_value=resp)
    return client


@pytest.mark.asyncio
async def test_set_vm_power_start_qemu_url_and_op():
    from backend.integrations.proxmox import ProxmoxData
    fake_data = ProxmoxData(vms=[{"vmid": 101, "name": "win11", "status": "stopped", "type": "qemu", "node": "pve"}])
    resp = _get_response({"data": "UPID:pve:..."})

    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=fake_data) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.invalidate = MagicMock()
        mock_client = _post_client(resp)
        mock_cls.return_value = mock_client
        from backend.integrations.proxmox import set_vm_power
        await set_vm_power(101, "start")

    called_url = mock_client.__aenter__.return_value.post.call_args.args[0]
    assert called_url == "https://192.168.1.60:8006/api2/json/nodes/pve/qemu/101/status/start"


@pytest.mark.asyncio
async def test_set_vm_power_stop_maps_to_shutdown_lxc():
    """action='stop' must map to Proxmox's graceful 'shutdown' op, and the
    URL must use /lxc/ for an lxc-typed vmid, never assumed as qemu."""
    from backend.integrations.proxmox import ProxmoxData
    fake_data = ProxmoxData(vms=[{"vmid": 200, "name": "hermes", "status": "running", "type": "lxc", "node": "pve"}])
    resp = _get_response({"data": "UPID:pve:..."})

    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=fake_data) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.invalidate = MagicMock()
        mock_client = _post_client(resp)
        mock_cls.return_value = mock_client
        from backend.integrations.proxmox import set_vm_power
        await set_vm_power(200, "stop")

    called_url = mock_client.__aenter__.return_value.post.call_args.args[0]
    assert called_url == "https://192.168.1.60:8006/api2/json/nodes/pve/lxc/200/status/shutdown"


@pytest.mark.asyncio
async def test_set_vm_power_reboot():
    from backend.integrations.proxmox import ProxmoxData
    fake_data = ProxmoxData(vms=[{"vmid": 101, "name": "win11", "status": "running", "type": "qemu", "node": "pve"}])
    resp = _get_response({"data": "UPID:pve:..."})

    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=fake_data) as mock_fetch, \
         patch("httpx.AsyncClient") as mock_cls:
        mock_fetch.invalidate = MagicMock()
        mock_client = _post_client(resp)
        mock_cls.return_value = mock_client
        from backend.integrations.proxmox import set_vm_power
        await set_vm_power(101, "reboot")

    called_url = mock_client.__aenter__.return_value.post.call_args.args[0]
    assert called_url.endswith("/status/reboot")


@pytest.mark.asyncio
async def test_set_vm_power_unknown_vmid_raises_before_any_http_call():
    from backend.integrations.proxmox import ProxmoxData, set_vm_power
    fake_data = ProxmoxData(vms=[{"vmid": 101, "name": "win11", "status": "running", "type": "qemu", "node": "pve"}])

    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=fake_data), \
         patch("httpx.AsyncClient") as mock_cls:
        with pytest.raises(ValueError, match="unknown Proxmox vmid"):
            await set_vm_power(999, "start")
        mock_cls.assert_not_called()


@pytest.mark.asyncio
async def test_set_vm_power_invalid_action_raises_before_any_lookup():
    from backend.integrations.proxmox import set_vm_power
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock) as mock_fetch:
        with pytest.raises(ValueError, match="unknown vm power action"):
            await set_vm_power(101, "destroy")
        mock_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_vm_power_http_error_raises():
    from backend.integrations.proxmox import ProxmoxData
    fake_data = ProxmoxData(vms=[{"vmid": 101, "name": "win11", "status": "running", "type": "qemu", "node": "pve"}])
    resp = _get_response(None, status_code=500)
    resp.raise_for_status.side_effect = Exception("HTTP 500")

    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=fake_data), \
         patch("httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = _post_client(resp)
        from backend.integrations.proxmox import set_vm_power
        with pytest.raises(RuntimeError, match="Proxmox power action failed"):
            await set_vm_power(101, "start")


@pytest.mark.asyncio
async def test_set_vm_power_no_token_raises(_token_configured):
    _token_configured.proxmox_token = ""
    from backend.integrations.proxmox import set_vm_power
    with patch("httpx.AsyncClient") as mock_cls:
        with pytest.raises(RuntimeError, match="PROXMOX_TOKEN"):
            await set_vm_power(101, "start")
        mock_cls.assert_not_called()
