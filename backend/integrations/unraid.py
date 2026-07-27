import asyncio
import logging
import re
from dataclasses import dataclass, field

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)

# Docker container ids/names are alphanumeric + `_.-` (Docker's own naming rule).
# restart_docker() splices container_id into a GraphQL query string via an
# f-string (no query variables) -- anything outside this set could break out
# of the string literal. Callers include an LLM tool-call arg (write_tools.py)
# with no charset check of its own, so this must be enforced at the sink, not
# just at one call site.
_SAFE_CONTAINER_ID = re.compile(r"^[A-Za-z0-9_.:-]+$")

_GQL_QUERY = """
{
  array {
    state
    disks { name size fsUsed type temp status }
    parities { name status }
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
            # Unraid's GraphQL schema puts parity drives in their own top-level
            # `parities` list, never inside `disks` (confirmed via schema
            # introspection — no `disks[].type == "PARITY"` entry has ever
            # existed on a real server; that filter was permanently vacuous
            # and parity_status silently stuck at its "unknown" default since
            # this integration was written, until the contract canary caught it).
            parities = arr.get("parities", [])
            if parities:
                data.parity_status = (parities[0].get("status") or "unknown").lower()

            # size/fsUsed are in KB
            data_disks = [d for d in disks if d.get("type") == "DATA"]
            total_kb = sum(d.get("size", 0) for d in data_disks)
            used_kb = sum(d.get("fsUsed", 0) for d in data_disks)
            data.storage_total_gb = round(total_kb / 1048576, 1)
            data.storage_used_gb = round(used_kb / 1048576, 1)

            containers = gql.get("docker", {}).get("containers", [])
            data.docker_containers = [
                {
                    # Real Unraid container ids are 129-char <hash>:<hash>
                    # PrefixedIDs -- a [:12] truncation here used to collapse
                    # every container to an identical shared prefix (fixed
                    # 2026-07-27, Phase 7c). Telegram callback_data still
                    # carries the NAME, never this id -- see resolve_container_id.
                    "id": c.get("id", ""),
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


async def resolve_container_id(name_or_id: str) -> str:
    """Resolve a container NAME or a real id to the real id.

    Real ids are 129-char '<hash>:<hash>' PrefixedIDs -- past Telegram's
    64-byte callback_data limit, so callers keep the container NAME in
    callback_data and only need the real id at the point of dispatch
    (see homelab_watch.py, telegram_poller.py). If the input already
    contains ':' (the real id shape) it's passed through as-is; otherwise
    it's resolved by exact name match against the current container list.

    Raises ValueError on an unsafe/empty input, an unknown name, or an
    ambiguous name (matches more than one container) -- never guesses.
    """
    if not name_or_id or not _SAFE_CONTAINER_ID.match(name_or_id):
        raise ValueError(f"unsafe or empty container reference: {name_or_id!r}")
    if ":" in name_or_id:
        return name_or_id

    data = await fetch()
    matches = [c for c in data.docker_containers if c.get("name") == name_or_id]
    if not matches:
        raise ValueError(f"no container found with name {name_or_id!r}")
    if len(matches) > 1:
        raise ValueError(f"ambiguous container name {name_or_id!r}: matches {len(matches)} containers")
    return matches[0]["id"]


async def _docker_mutation(op: str, container_id: str) -> tuple[bool, str]:
    """Run one docker { <op>(id: ...) { id state } } mutation. Returns
    (ok, error_message) -- never raises, so restart_docker can distinguish
    stop-failed from start-failed."""
    from backend.config import get_settings
    settings = get_settings()
    api_key = settings.unraid_api_key
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    mutation = f'mutation {{ docker {{ {op}(id: "{container_id}") {{ id state }} }} }}'
    try:
        async with httpx.AsyncClient(timeout=10, verify=False) as client:  # nosec B501
            resp = await client.post(
                f"https://{settings.unraid_host}/graphql",
                json={"query": mutation},
                headers=headers,
            )
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}"
            body = resp.json()
            if body.get("errors"):
                return False, str(body["errors"])
            return True, ""
    except Exception as e:
        return False, str(e)


async def restart_docker(name_or_id: str) -> dict:
    """Restart a Docker container: resolve, stop, poll until actually
    stopped (up to ~5s), then start.

    No `restartContainer` mutation exists in Unraid's GraphQL schema
    (confirmed live 2026-07-27) -- this is genuinely two separate calls, so
    a stop that succeeds followed by a start that fails leaves the
    container DOWN, not merely 'not restarted'. That state must never look
    like a plain, recoverable failure to a caller.

    Returns:
      {"success": True} on a clean restart.
      {"success": False, "error": ...} if resolution or the stop call
        itself failed -- the container is presumed untouched.
      {"success": False, "stopped": True, "error": ...} if stop succeeded
        but start then failed -- the container is confirmed DOWN; callers
        must surface this as urgent, not routine.
    """
    try:
        container_id = await resolve_container_id(name_or_id)
    except ValueError as e:
        logger.warning(f"restart_docker: {e}")
        return {"success": False, "error": str(e)}

    ok, err = await _docker_mutation("stop", container_id)
    if not ok:
        return {"success": False, "error": f"stop failed: {err}"}

    # Poll for the container to actually report stopped -- starting it again
    # while it's still mid-stop is the likely failure mode of firing start
    # immediately. Each iteration invalidates the cache so it isn't reading
    # the same 30s-stale snapshot five times in a row.
    for _ in range(5):
        fetch.invalidate()
        data = await fetch()
        c = next((c for c in data.docker_containers if c.get("id") == container_id), None)
        if c and (c.get("state") or "").upper() != "RUNNING":
            break
        await asyncio.sleep(1)

    ok, err = await _docker_mutation("start", container_id)
    fetch.invalidate()
    if not ok:
        return {"success": False, "stopped": True, "error": f"stopped but failed to restart: {err}"}
    return {"success": True}
