import logging
from dataclasses import dataclass, field

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)

_GQL_QUERY = """
{
  array {
    state
    disks { name size fsUsed type temp status }
  }
  docker {
    containers { id names state status }
  }
}
"""


@dataclass
class UnraidData:
    array_status: str = "unknown"
    parity_status: str = "unknown"
    mover_running: bool = False
    disk_health: list = field(default_factory=list)
    docker_containers: list = field(default_factory=list)
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    storage_used_gb: float = 0.0
    storage_total_gb: float = 0.0


@async_ttl_cache(30)
async def fetch() -> UnraidData:
    from backend.config import get_settings
    settings = get_settings()
    try:
        api_key = settings.unraid_api_key
    except Exception:
        raise Exception("UNRAID_API_KEY not configured")

    url = f"https://{settings.unraid_host}/graphql"
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}

    data = UnraidData()
    async with httpx.AsyncClient(timeout=5, verify=False) as client:  # nosec B501 — Unraid self-signed cert
        try:
            resp = await client.post(url, json={"query": _GQL_QUERY}, headers=headers)
            resp.raise_for_status()
            gql = resp.json().get("data", {})

            arr = gql.get("array", {})
            data.array_status = arr.get("state", "unknown").lower()
            disks = arr.get("disks", [])
            data.disk_health = [
                {"name": d["name"], "temp": d.get("temp"), "status": d.get("status", "")}
                for d in disks
            ]
            parity_disks = [d for d in disks if d.get("type") == "PARITY"]
            if parity_disks:
                data.parity_status = parity_disks[0].get("status", "unknown").lower()

            # size/fsUsed are in KB
            data_disks = [d for d in disks if d.get("type") == "DATA"]
            total_kb = sum(d.get("size", 0) for d in data_disks)
            used_kb = sum(d.get("fsUsed", 0) for d in data_disks)
            data.storage_total_gb = round(total_kb / 1048576, 1)
            data.storage_used_gb = round(used_kb / 1048576, 1)

            containers = gql.get("docker", {}).get("containers", [])
            data.docker_containers = [
                {
                    "id": c.get("id", "")[:12],
                    "name": (c.get("names") or [""])[0].lstrip("/"),
                    "status": c.get("status", ""),
                    "state": c.get("state", ""),
                }
                for c in containers
            ]
        except Exception as e:
            # A failed/incomplete read must NOT be reported as real zeros — a
            # zero-filled UnraidData (storage 0.0/0.0, array "unknown", 0 docker)
            # looks like CATASTROPHIC DATA LOSS to the briefing/trends/proposer and
            # fires false "storage zeroed / massive negative trend / ANOMALY" alerts.
            # Raise instead so callers (gather(return_exceptions=True)) treat Unraid
            # as UNAVAILABLE — the cache caches+re-raises the exception briefly.
            logger.warning(f"Unraid fetch failed (reporting unavailable): {e}")
            raise RuntimeError(f"Unraid unavailable: {e}") from e

    return data


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        api_key = settings.unraid_api_key
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=5, verify=False) as client:  # nosec B501
            resp = await client.post(
                f"https://{settings.unraid_host}/graphql",
                json={"query": "{ array { state } }"},
                headers=headers,
            )
            return resp.status_code == 200 and "data" in resp.json()
    except Exception:
        return False


async def restart_docker(container_id: str) -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        api_key = settings.unraid_api_key
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}
        mutation = f'mutation {{ restartContainer(id: "{container_id}") {{ success }} }}'
        async with httpx.AsyncClient(timeout=5, verify=False) as client:  # nosec B501
            resp = await client.post(
                f"https://{settings.unraid_host}/graphql",
                json={"query": mutation},
                headers=headers,
            )
            # Force the next dashboard poll to show the container's new state
            # instead of the cached pre-restart snapshot.
            fetch.invalidate()
            return resp.status_code == 200
    except Exception:
        return False
