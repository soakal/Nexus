import logging
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
