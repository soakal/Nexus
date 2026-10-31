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
        {"type": "lxc", "vmid": 200, "name": "jellyfin-vm", "status": "running"},
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
# set_vm_power (native)
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
    fake_data = ProxmoxData(vms=[{"vmid": 200, "name": "jellyfin-vm", "status": "running", "type": "lxc", "node": "pve"}])
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


# ---------------------------------------------------------------------------
# Graph topology readers
# ---------------------------------------------------------------------------

def _multi_client(*resps):
    client = AsyncMock()
    client.__aenter__.return_value.get = AsyncMock(side_effect=list(resps))
    return client


def _rows():
    from backend.integrations.proxmox import ProxmoxData
    return ProxmoxData(node="pve", vms=[
        {"vmid": 103, "name": "a", "type": "lxc", "node": "pve"},
        {"vmid": 109, "name": "b", "type": "qemu", "node": "pve"},
    ])


def test_parse_pve_kv_and_nic_hwaddr():
    from backend.integrations.proxmox import _nic_hwaddr, _parse_pve_kv
    assert _parse_pve_kv("VM:subvol-103-disk-0,size=10G") == ("VM:subvol-103-disk-0", {"size": "10G"})
    assert _parse_pve_kv("name=eth0,bridge=vmbr0") == (None, {"name": "eth0", "bridge": "vmbr0"})
    assert _parse_pve_kv("/mnt/pve/Movies,mp=/movies")[1] == {"mp": "/movies"}
    assert _nic_hwaddr(_parse_pve_kv("virtio=bc:24:11:aa:bb:cc,bridge=vmbr0")[1]) == "BC:24:11:AA:BB:CC"
    assert _nic_hwaddr(_parse_pve_kv("e1000=BC:24:11:AA:BB:CD,bridge=vmbr0")[1]) == "BC:24:11:AA:BB:CD"
    assert _nic_hwaddr(_parse_pve_kv("name=eth0,hwaddr=BC:24:11:AA:BB:CE,ip=dhcp")[1]) == "BC:24:11:AA:BB:CE"
    assert _nic_hwaddr({"bridge": "vmbr0"}) is None


@pytest.mark.asyncio
async def test_fetch_guest_configs_parses_both_types():
    lxc = {"hostname": "jelly", "onboot": 1, "cores": 2, "memory": 2048, "tags": "a;b",
           "rootfs": "VM:subvol-103-disk-0,size=10G", "mp0": "/mnt/pve/Movies,mp=/movies",
           "net0": "name=eth0,bridge=vmbr0,hwaddr=BC:24:11:AA:BB:CC,ip=192.168.1.62/24"}
    qemu = {"name": "devbox", "scsi0": "VM:vm-109-disk-0,size=50G", "ide0": "none,media=cdrom",
            "ide2": "VM:vm-109-cloudinit,media=cdrom", "net0": "virtio=BC:24:11:AA:BB:DD,bridge=vmbr0,firewall=1"}
    client = _multi_client(_get_response(lxc), _get_response(qemu))
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=_rows()), \
         patch("httpx.AsyncClient", return_value=client):
        from backend.integrations.proxmox import fetch_guest_configs
        out = await fetch_guest_configs()
    urls = [c.args[0] for c in client.__aenter__.return_value.get.call_args_list]
    assert urls == ["https://192.168.1.60:8006/api2/json/nodes/pve/lxc/103/config",
                    "https://192.168.1.60:8006/api2/json/nodes/pve/qemu/109/config"]
    assert out[103]["name"] == "jelly" and out[103]["tags"] == ["a", "b"] and out[103]["cores"] == 2
    assert [v["volume"] for v in out[103]["volumes"]] == ["/mnt/pve/Movies", "VM:subvol-103-disk-0"]
    assert out[103]["volumes"][0]["mp"] == "/movies"
    assert out[103]["volumes"][0]["backup"] is False and out[103]["volumes"][1]["backup"] is True  # mp0 bind vs rootfs
    assert out[103]["net"] == [{"key": "net0", "ip": "192.168.1.62/24", "bridge": "vmbr0", "hwaddr": "BC:24:11:AA:BB:CC"}]
    assert out[109]["type"] == "qemu" and out[109]["cores"] is None
    assert [v["key"] for v in out[109]["volumes"]] == ["ide2", "scsi0"]  # ide0: none skipped
    assert all(v["backup"] for v in out[109]["volumes"])
    assert out[109]["net"][0]["hwaddr"] == "BC:24:11:AA:BB:DD" and out[109]["net"][0]["ip"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["http500", "empty"])
async def test_fetch_guest_configs_raises_never_partial(bad):
    ok = _get_response({"hostname": "x", "rootfs": "VM:v,size=1G"})
    if bad == "http500":
        broken = _get_response(None, status_code=500)
        broken.raise_for_status.side_effect = Exception("HTTP 500")
    else:
        broken = _get_response({})
    with patch("backend.integrations.proxmox.fetch", new_callable=AsyncMock, return_value=_rows()), \
         patch("httpx.AsyncClient", return_value=_multi_client(ok, broken)):
        from backend.integrations.proxmox import fetch_guest_configs
        with pytest.raises(RuntimeError):
            await fetch_guest_configs()


@pytest.mark.asyncio
async def test_fetch_guest_configs_no_token_raises(_token_configured):
    _token_configured.proxmox_token = ""
    from backend.integrations.proxmox import fetch_guest_configs
    with pytest.raises(RuntimeError, match="PROXMOX_TOKEN"):
        await fetch_guest_configs()


@pytest.mark.asyncio
async def test_fetch_storages_parses_disable_and_has_no_status():
    rows = [{"storage": "local", "type": "dir", "content": "iso"},
            {"storage": "VM_BU", "type": "cifs", "server": "192.168.1.50", "share": "Vm_Backup", "disable": 1},
            {"storage": "VM_BU_NFS", "type": "nfs", "server": "192.168.1.50", "export": "/mnt/user/Vm_Backup"}]
    with patch("httpx.AsyncClient", return_value=_get_client(_get_response(rows))):
        from backend.integrations.proxmox import fetch_storages
        out = await fetch_storages()
    assert [s["storage"] for s in out] == ["local", "VM_BU", "VM_BU_NFS"]
    assert [s["disabled"] for s in out] == [False, True, False]
    assert out[1]["export"] == "Vm_Backup" and out[2]["export"] == "/mnt/user/Vm_Backup"
    assert out[0]["server"] is None
    assert all("status" not in s for s in out)


@pytest.mark.asyncio
async def test_fetch_storages_empty_raises():
    with patch("httpx.AsyncClient", return_value=_get_client(_get_response([]))):
        from backend.integrations.proxmox import fetch_storages
        with pytest.raises(RuntimeError):
            await fetch_storages()


def _children(*ids):
    return _get_response({"children": [{"id": i, "type": "lxc"} for i in ids]})


@pytest.mark.asyncio
async def test_fetch_backup_jobs_enabled_job_and_urls():
    job = {"id": "backup-1", "type": "vzdump", "enabled": 1, "all": 1, "storage": "VM_BU_NFS", "schedule": "21:00"}
    client = _multi_client(_get_response([job]), _children(101, 102, 103))
    with patch("httpx.AsyncClient", return_value=client):
        from backend.integrations.proxmox import fetch_backup_jobs
        out = await fetch_backup_jobs()
    assert out == [{"id": "backup-1", "storage": "VM_BU_NFS", "schedule": "21:00", "all": True, "vmids": [101, 102, 103]}]
    urls = [c.args[0] for c in client.__aenter__.return_value.get.call_args_list]
    assert urls == ["https://192.168.1.60:8006/api2/json/cluster/backup",
                    "https://192.168.1.60:8006/api2/json/cluster/backup/backup-1/included_volumes"]


@pytest.mark.asyncio
async def test_fetch_backup_jobs_disabled_skipped_and_missing_enabled_means_on():
    off = {"id": "off", "type": "vzdump", "enabled": 0, "storage": "S"}
    client = _multi_client(_get_response([off]))
    with patch("httpx.AsyncClient", return_value=client):
        from backend.integrations.proxmox import fetch_backup_jobs
        assert await fetch_backup_jobs() == []
    assert client.__aenter__.return_value.get.await_count == 1

    from backend.cache import reset_all_caches
    reset_all_caches()
    on = {"id": "on", "type": "vzdump", "storage": "S"}  # no 'enabled' key
    with patch("httpx.AsyncClient", return_value=_multi_client(_get_response([on]), _children(7))):
        out = await fetch_backup_jobs()
    assert out[0]["vmids"] == [7] and out[0]["all"] is False


@pytest.mark.asyncio
async def test_fetch_backup_jobs_empty_list_is_valid():
    with patch("httpx.AsyncClient", return_value=_multi_client(_get_response([]))):
        from backend.integrations.proxmox import fetch_backup_jobs
        assert await fetch_backup_jobs() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["no_data", "http500_list", "http500_inc", "no_children"])
async def test_fetch_backup_jobs_malformed_raises(case):
    job = {"id": "j", "type": "vzdump", "storage": "S"}
    bad500 = _get_response(None, status_code=500)
    bad500.raise_for_status.side_effect = Exception("HTTP 500")
    nodata = MagicMock(status_code=200)
    nodata.raise_for_status = MagicMock()
    nodata.json.return_value = {}
    resps = {
        "no_data": [nodata],
        "http500_list": [bad500],
        "http500_inc": [_get_response([job]), bad500],
        "no_children": [_get_response([job]), _get_response({})],
    }[case]
    with patch("httpx.AsyncClient", return_value=_multi_client(*resps)):
        from backend.integrations.proxmox import fetch_backup_jobs
        with pytest.raises(RuntimeError):
            await fetch_backup_jobs()


@pytest.mark.asyncio
async def test_fetch_backup_jobs_no_token_raises(_token_configured):
    _token_configured.proxmox_token = ""
    from backend.integrations.proxmox import fetch_backup_jobs
    with pytest.raises(RuntimeError, match="PROXMOX_TOKEN"):
        await fetch_backup_jobs()
