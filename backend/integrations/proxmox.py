import logging
import re
from dataclasses import dataclass, field

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)

_BYTES_PER_GIB = 1024 ** 3


@dataclass
class ProxmoxData:
    node: str = ""
    node_status: str = "unknown"
    cpu_pct: float = 0.0
    mem_used_gb: float = 0.0
    mem_total_gb: float = 0.0
    vms: list = field(default_factory=list)
    storage_used_gb: float = 0.0
    storage_total_gb: float = 0.0


@async_ttl_cache(30)
async def fetch() -> ProxmoxData:
    from backend.config import get_settings
    settings = get_settings()

    token = settings.proxmox_token
    if not token:
        raise RuntimeError("Proxmox unavailable: PROXMOX_TOKEN not configured")

    url = f"{settings.proxmox_host}/api2/json/cluster/resources"
    headers = {"Authorization": token}

    data = ProxmoxData()
    async with httpx.AsyncClient(timeout=5, verify=False) as client:  # nosec B501 — Proxmox self-signed cert
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            rows = resp.json().get("data")
            if not rows:
                # No resource rows at all means we can't trust ANY field — a
                # zero-filled ProxmoxData (mem 0/0, storage 0/0, no VMs) looks
                # like a dead node to the briefing/trends/proposer and fires
                # false "storage zeroed / node down" alerts. Raise instead so
                # callers treat Proxmox as UNAVAILABLE (the Unraid lesson).
                raise ValueError("cluster/resources returned no rows")

            for r in rows:
                rtype = r.get("type")
                if rtype == "node":
                    data.node = r.get("node", "") or data.node
                    data.node_status = r.get("status", "unknown")
                    # cpu is a 0-1 fraction; mem/maxmem are bytes.
                    data.cpu_pct = round(float(r.get("cpu", 0.0)) * 100, 1)
                    data.mem_used_gb = round(float(r.get("mem", 0)) / _BYTES_PER_GIB, 1)
                    data.mem_total_gb = round(float(r.get("maxmem", 0)) / _BYTES_PER_GIB, 1)
                elif rtype in ("qemu", "lxc"):
                    data.vms.append({
                        "vmid": r.get("vmid"),
                        "name": r.get("name", ""),
                        "status": r.get("status", "unknown"),
                        "type": rtype,
                        "node": r.get("node"),
                    })
                elif rtype == "storage":
                    data.storage_used_gb += float(r.get("disk", 0)) / _BYTES_PER_GIB
                    data.storage_total_gb += float(r.get("maxdisk", 0)) / _BYTES_PER_GIB

            data.storage_used_gb = round(data.storage_used_gb, 1)
            data.storage_total_gb = round(data.storage_total_gb, 1)
        except Exception as e:
            logger.warning(f"Proxmox fetch failed (reporting unavailable): {e}")
            raise RuntimeError(f"Proxmox unavailable: {e}") from e

    return data


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        token = settings.proxmox_token
        if not token:
            # Unconfigured shows OFFLINE, never crashes.
            return False
        headers = {"Authorization": token}
        async with httpx.AsyncClient(timeout=5, verify=False) as client:  # nosec B501
            resp = await client.get(
                f"{settings.proxmox_host}/api2/json/version",
                headers=headers,
            )
            return resp.status_code == 200 and bool(resp.json().get("data"))
    except Exception:
        return False


_POWER_OPS = {"start": "start", "reboot": "reboot", "stop": "shutdown"}


async def set_vm_power(vmid: int, action: str) -> dict:
    """Start/reboot/gracefully-shut-down a Proxmox VM or LXC.

    Resolves vmid against fetch() to get both `node` and `type` (qemu vs
    lxc) rather than assuming either, since the API path differs:
    /nodes/{node}/qemu/{vmid}/... vs /nodes/{node}/lxc/{vmid}/....

    `action="stop"` deliberately maps to Proxmox's graceful `shutdown` op,
    not the raw power-pull `stop` op -- that harsher operation is not
    exposed here on purpose.

    Raises ValueError for an unknown vmid or action (before any HTTP call),
    RuntimeError on a non-2xx response -- same never-guess discipline as
    fetch_updates()/fetch_backups().
    """
    if action not in _POWER_OPS:
        raise ValueError(f"unknown vm power action: {action!r} (expected one of {sorted(_POWER_OPS)})")

    from backend.config import get_settings
    settings = get_settings()
    token = settings.proxmox_token
    if not token:
        raise RuntimeError("Proxmox unavailable: PROXMOX_TOKEN not configured")

    data = await fetch()
    vm = next((v for v in data.vms if v.get("vmid") == vmid), None)
    if vm is None:
        raise ValueError(f"unknown Proxmox vmid: {vmid!r}")
    node = vm.get("node")
    vm_type = vm.get("type")
    if not node or vm_type not in ("qemu", "lxc"):
        raise RuntimeError(f"Proxmox vmid {vmid} has no usable node/type: {vm!r}")

    op = _POWER_OPS[action]
    url = f"{settings.proxmox_host}/api2/json/nodes/{node}/{vm_type}/{vmid}/status/{op}"
    headers = {"Authorization": token}

    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:  # nosec B501 — Proxmox self-signed cert
            resp = await client.post(url, headers=headers)
            resp.raise_for_status()
            result = resp.json()
    except Exception as e:
        logger.warning(f"Proxmox set_vm_power failed (vmid={vmid}, action={action}): {e}")
        raise RuntimeError(f"Proxmox power action failed: {e}") from e

    fetch.invalidate()
    return result


@async_ttl_cache(900, falsy_ttl=60)
async def fetch_updates() -> dict:
    """Pending apt package updates on the Proxmox node.

    Raises RuntimeError on any failure (missing token, transport, non-200) —
    same never-zero-default discipline as fetch(). A genuinely empty package
    list (count == 0, up to date) is a real, valid result, not a failure."""
    from backend.config import get_settings
    settings = get_settings()

    token = settings.proxmox_token
    if not token:
        raise RuntimeError("Proxmox unavailable: PROXMOX_TOKEN not configured")

    node = (await fetch()).node
    if not node:
        raise RuntimeError("Proxmox unavailable: no node name from cluster/resources")

    url = f"{settings.proxmox_host}/api2/json/nodes/{node}/apt/update"
    headers = {"Authorization": token}

    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:  # nosec B501 — Proxmox self-signed cert
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            pkgs = resp.json().get("data") or []
    except Exception as e:
        logger.warning(f"Proxmox fetch_updates failed (reporting unavailable): {e}")
        raise RuntimeError(f"Proxmox unavailable: {e}") from e

    names = [p.get("Package", "") for p in pkgs[:15]]
    return {"node": node, "count": len(pkgs), "packages": names}


@async_ttl_cache(900, falsy_ttl=60)
async def fetch_backups() -> dict:
    """Most recent vzdump (backup) task status on the Proxmox node.

    Raises RuntimeError on any failure — same never-zero-default discipline as
    fetch(). No vzdump tasks at all is a real result ("none"), not a failure."""
    from backend.config import get_settings
    settings = get_settings()

    token = settings.proxmox_token
    if not token:
        raise RuntimeError("Proxmox unavailable: PROXMOX_TOKEN not configured")

    node = (await fetch()).node
    if not node:
        raise RuntimeError("Proxmox unavailable: no node name from cluster/resources")

    url = f"{settings.proxmox_host}/api2/json/nodes/{node}/tasks"
    headers = {"Authorization": token}

    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:  # nosec B501 — Proxmox self-signed cert
            resp = await client.get(
                url, headers=headers, params={"typefilter": "vzdump", "limit": 5}
            )
            resp.raise_for_status()
            tasks = resp.json().get("data") or []
    except Exception as e:
        logger.warning(f"Proxmox fetch_backups failed (reporting unavailable): {e}")
        raise RuntimeError(f"Proxmox unavailable: {e}") from e

    tasks = [t for t in tasks if t.get("type") == "vzdump"]
    if not tasks:
        return {"node": node, "status": "none"}

    tasks.sort(key=lambda t: t.get("starttime", 0), reverse=True)
    latest = tasks[0]
    raw_status = latest.get("status")
    if raw_status is None:
        status = "running"
    elif raw_status == "OK":
        status = "ok"
    else:
        status = "failed"

    return {
        "node": node,
        "status": status,
        "detail": raw_status or "",
        "endtime": latest.get("endtime") or latest.get("starttime"),
    }


# ---------------------------------------------------------------------------
# Graph topology readers (strict: RuntimeError on any failure, never partial)
# ---------------------------------------------------------------------------

_VOLUME_KEY = re.compile(r"^(rootfs|mp\d+|scsi\d+|virtio\d+|sata\d+|ide\d+|efidisk\d+|tpmstate\d+)$")
_NET_KEY = re.compile(r"^net\d+$")
_QEMU_NIC_MODELS = frozenset({"virtio", "e1000", "e1000e", "rtl8139", "vmxnet3"})


def _parse_pve_kv(value: str) -> tuple[str | None, dict[str, str]]:
    """'VM:subvol-103-disk-0,size=10G' -> ('VM:subvol-103-disk-0', {'size': '10G'});
    'name=eth0,bridge=vmbr0' -> (None, {...}). A leading token without '=' is the volume."""
    parts = [p for p in str(value).split(",") if p]
    head = None
    if parts and "=" not in parts[0]:
        head = parts.pop(0)
    kv = {}
    for p in parts:
        k, _, v = p.partition("=")
        kv[k] = v
    return head, kv


def _nic_hwaddr(kv: dict[str, str]) -> str | None:
    # LXC: hwaddr=...; QEMU: the MAC is the VALUE of the model key (virtio=BC:..).
    mac = kv.get("hwaddr") or next((v for k, v in kv.items() if k in _QEMU_NIC_MODELS), None)
    return mac.upper() if mac else None


def _opt_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_guest_config(vm_type: str, cfg: dict) -> dict:
    volumes, nets = [], []
    for key in sorted(cfg):
        if _VOLUME_KEY.match(key):
            vol, kv = _parse_pve_kv(cfg[key])
            if not vol or vol == "none":
                continue
            volumes.append({
                "key": key, "volume": vol, "mp": kv.get("mp"),
                # PVE default: backed up for qemu drives and rootfs; an LXC mp<N> needs explicit backup=1
                "backup": kv.get("backup", "1" if vm_type == "qemu" or key == "rootfs" else "0") not in ("0", "no", "off", "false"),
            })
        elif _NET_KEY.match(key):
            _, kv = _parse_pve_kv(cfg[key])
            nets.append({"key": key, "ip": kv.get("ip"), "bridge": kv.get("bridge"), "hwaddr": _nic_hwaddr(kv)})
    return {
        "name": cfg.get("hostname") or cfg.get("name") or "",
        "type": vm_type,
        "onboot": _opt_int(cfg.get("onboot")) or 0,
        "cores": _opt_int(cfg.get("cores")),
        "memory": _opt_int(cfg.get("memory")),
        "tags": [t for t in re.split(r"[;,]", str(cfg.get("tags") or "")) if t],
        "hookscript": cfg.get("hookscript"),
        "volumes": volumes,
        "net": nets,
    }


@async_ttl_cache(600, falsy_ttl=60)
async def fetch_guest_configs() -> dict[int, dict]:
    """Per-guest config (volumes, NICs, tags) for every guest in cluster/resources.
    Sequential GETs; any failure raises RuntimeError (a partial map would read as
    'guest vanished' to the graph)."""
    from backend.config import get_settings
    settings = get_settings()
    token = settings.proxmox_token
    if not token:
        raise RuntimeError("Proxmox unavailable: PROXMOX_TOKEN not configured")

    data = await fetch()
    headers = {"Authorization": token}
    out: dict[int, dict] = {}
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:  # nosec B501 — Proxmox self-signed cert
            for vm in data.vms:
                vmid, vm_type = vm.get("vmid"), vm.get("type")
                node = vm.get("node") or data.node
                url = f"{settings.proxmox_host}/api2/json/nodes/{node}/{vm_type}/{vmid}/config"
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                cfg = resp.json().get("data")
                if not cfg or not isinstance(cfg, dict):
                    raise ValueError(f"empty config for {vm_type}/{vmid}")
                out[int(vmid)] = _parse_guest_config(vm_type, cfg)
    except Exception as e:
        logger.warning(f"Proxmox fetch_guest_configs failed (reporting unavailable): {e}")
        raise RuntimeError(f"Proxmox unavailable: {e}") from e
    return out


@async_ttl_cache(600, falsy_ttl=60)
async def fetch_storages() -> list[dict]:
    """Configured storages from /storage (identity + backing server only; no usage)."""
    from backend.config import get_settings
    settings = get_settings()
    token = settings.proxmox_token
    if not token:
        raise RuntimeError("Proxmox unavailable: PROXMOX_TOKEN not configured")
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:  # nosec B501
            resp = await client.get(f"{settings.proxmox_host}/api2/json/storage", headers={"Authorization": token})
            resp.raise_for_status()
            rows = resp.json().get("data")
        if not rows or not isinstance(rows, list):
            raise ValueError("storage list is empty")  # a node always has 'local'
        return [{
            "storage": r["storage"],
            "type": r.get("type"),
            "server": r.get("server"),
            "export": r.get("export") or r.get("share"),
            "content": r.get("content", ""),
            "disabled": bool(r.get("disable", 0)),
        } for r in rows]
    except Exception as e:
        logger.warning(f"Proxmox fetch_storages failed (reporting unavailable): {e}")
        raise RuntimeError(f"Proxmox unavailable: {e}") from e


@async_ttl_cache(600, falsy_ttl=60)
async def fetch_backup_jobs() -> list[dict]:
    """Enabled vzdump jobs with the guests PVE resolved into each (live source of
    backs_up_to). An empty job list is a valid result; anything malformed raises."""
    from backend.config import get_settings
    settings = get_settings()
    token = settings.proxmox_token
    if not token:
        raise RuntimeError("Proxmox unavailable: PROXMOX_TOKEN not configured")
    headers = {"Authorization": token}
    base = f"{settings.proxmox_host}/api2/json/cluster/backup"
    jobs: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:  # nosec B501
            resp = await client.get(base, headers=headers)
            resp.raise_for_status()
            body = resp.json()
            rows = body.get("data") if isinstance(body, dict) else None
            if not isinstance(rows, list):
                raise ValueError("cluster/backup returned no job list")
            for job in rows:
                if job.get("type") != "vzdump" or not bool(job.get("enabled", 1)):
                    continue
                resp = await client.get(f"{base}/{job['id']}/included_volumes", headers=headers)
                resp.raise_for_status()
                inc = resp.json().get("data")
                if not isinstance(inc, dict) or not isinstance(inc.get("children"), list):
                    raise ValueError(f"included_volumes for {job['id']} has no children")
                jobs.append({
                    "id": job["id"],
                    "storage": job.get("storage"),
                    "schedule": job.get("schedule"),
                    "all": bool(job.get("all", 0)),
                    "vmids": [int(c["id"]) for c in inc["children"]],
                })
    except Exception as e:
        logger.warning(f"Proxmox fetch_backup_jobs failed (reporting unavailable): {e}")
        raise RuntimeError(f"Proxmox unavailable: {e}") from e
    return jobs
