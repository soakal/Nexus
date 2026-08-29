import asyncio
import io
import logging
import pathlib
import re
from dataclasses import dataclass, field

import httpx
import paramiko

from backend.cache import async_ttl_cache
from backend.integrations.unraid_tls_pinning import build_transport

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
  metrics {
    cpu { percentTotal }
    memory { percentTotal }
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
    async with httpx.AsyncClient(timeout=5, transport=build_transport()) as client:
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

            # Live GraphQL introspection (2026-07-29) confirmed `metrics.cpu.percentTotal`
            # and `metrics.memory.percentTotal` are both real, currently-populated
            # fields (Query -> Metrics -> CpuUtilization/MemoryUtilization) --
            # `__schema` is disabled server-side but `__type(name: "Metrics")` isn't.
            # memory.percentTotal is NOT used/total (that reads ~95% on a healthy box
            # thanks to Linux's disk-cache-heavy `buffcache`) -- verified live it
            # matches active/total, i.e. the same "actually in use, excluding
            # reclaimable cache" convention Unraid's own webGUI shows. Missing/absent
            # `metrics` degrades to the dataclass default (0.0), same as the existing
            # missing-`docker`-section tolerance below -- only a transport/HTTP
            # failure (caught by the outer except) is treated as unavailable.
            # `.get(key) or 0.0`, not `.get(key, 0.0)` -- GraphQL leaves are
            # nullable unless declared `!`, so an explicit `null` (not just a
            # missing key) is a real possibility and `.get(key, default)`
            # doesn't catch it: `round(None, 1)` raises TypeError, which the
            # outer except then wrongly reports as a full Unraid outage.
            metrics = gql.get("metrics") or {}
            cpu = metrics.get("cpu") or {}
            mem = metrics.get("memory") or {}
            data.cpu_pct = round(cpu.get("percentTotal") or 0.0, 1)
            data.ram_pct = round(mem.get("percentTotal") or 0.0, 1)

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
        async with httpx.AsyncClient(timeout=5, transport=build_transport()) as client:
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
        async with httpx.AsyncClient(timeout=10, transport=build_transport()) as client:
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
    # A container that has already crashed/exited (as opposed to being bounced
    # while still healthy) is already stopped, so this mutation 304s --
    # "(HTTP code 304) container already stopped" -- confirmed live 2026-08-28
    # via homelab_watch's own auto-restart path, which restarts a container
    # exactly when it's JUST been observed as no longer RUNNING, i.e. this is
    # the common case for that caller, not an edge case. Treating it as fatal
    # meant restart_docker could never actually recover a crashed container --
    # this affected the existing manual Telegram Restart button too, it just
    # never surfaced because every prior drill bounced a running container.
    # Any OTHER stop failure (permissions, network, a real API error) still
    # aborts here, untouched.
    already_stopped = not ok and "already stopped" in err.lower()
    if not ok and not already_stopped:
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


# ---------------------------------------------------------------------------
# Phase 7d — native docker prune (dangling images only) via SSH
#
# Live GraphQL introspection confirmed no prune mutation exists anywhere in
# Unraid's schema (root or nested) -- pruning has no REST/GraphQL surface at
# all on this server, only raw SSH.
#
# Deliberately DANGLING IMAGES ONLY (`docker image prune -f`), NOT the
# broader system-wide prune (docker's `system` + `prune` combo) -- Brian
# keeps some Unraid containers intentionally stopped, and a system-wide
# prune would delete those containers plus unused volumes/networks along
# with the images. This scope decision is inherited from the prior
# implementation, not invented here.
#
# The actual shell command is fixed SERVER-SIDE by an SSH forced-command
# restriction in Unraid's authorized_keys (out of scope for this codebase --
# installed manually on Unraid). `_PRUNE_SENTINEL` below is intentionally
# NOT a real, runnable command: this code sends a fixed sentinel string over
# SSH and relies entirely on the server-side forced-command mapping it to
# the real prune operation. If that server-side restriction is ever
# weakened or removed, this fails LOUDLY ("command not found") instead of
# silently becoming an unrestricted arbitrary-command channel.
# ---------------------------------------------------------------------------

_PRUNE_SENTINEL = "nexus-docker-prune"          # NOT the real command -- see docstring above
_SSH_KNOWN_HOSTS = pathlib.Path(".unraid_ssh_known_hosts")
_MAX_SSH_OUTPUT_BYTES = 65536


def _drain(stream) -> str:
    """Read a paramiko ChannelFile to genuine EOF, keeping only the first
    _MAX_SSH_OUTPUT_BYTES. Must fully drain (not just bounded-read) so the
    channel window empties and recv_exit_status() can't hang -- see the
    comment at its call site in _ssh_prune_sync.
    """
    chunks = []
    total = 0
    while True:
        chunk = stream.read(_MAX_SSH_OUTPUT_BYTES)
        if not chunk:
            break
        if total < _MAX_SSH_OUTPUT_BYTES:
            chunks.append(chunk)
            total += len(chunk)
    return b"".join(chunks)[:_MAX_SSH_OUTPUT_BYTES].decode("utf-8", errors="replace")


def _ssh_prune_sync() -> tuple[int, str, str]:
    """Blocking, synchronous SSH call -- run ONLY via asyncio.to_thread (never
    call this directly from a coroutine; paramiko has no async API).

    Returns (exit_status, stdout_text, stderr_text). Raises RuntimeError if
    UNRAID_SSH_PRIVATE_KEY or unraid_ssh_host isn't configured; any paramiko
    exception (AuthenticationException, SSHException, BadHostKeyException,
    socket errors, timeout) propagates as-is -- the caller (prune_docker_images)
    converts it to a RuntimeError.
    """
    from backend.config import get_settings
    settings = get_settings()

    try:
        pem = settings.unraid_ssh_private_key
    except Exception:
        raise RuntimeError("UNRAID_SSH_PRIVATE_KEY not configured")

    host = settings.unraid_ssh_host
    if not host:
        raise RuntimeError("UNRAID_SSH_HOST not configured")
    port = settings.unraid_ssh_port
    user = settings.unraid_ssh_user

    # Ed25519, not RSA, loaded from an in-memory string (io.StringIO), never a
    # file path -- the key material lives only in the vault, never touches disk.
    key = paramiko.Ed25519Key.from_private_key(io.StringIO(pem))

    client = paramiko.SSHClient()
    # The file must exist BEFORE any host-key API call, genuine first-run
    # or not: paramiko's AutoAddPolicy.missing_host_key() calls
    # save_host_keys(), which calls load_host_keys() first (to merge with
    # whatever's already there) -- and HostKeys.load() does a plain
    # open(filename, "r"), which raises FileNotFoundError on a file that's
    # never existed. Found live: a real first-ever connection crashed here
    # (paramiko's own docs don't mention this create-on-first-use gap).
    _SSH_KNOWN_HOSTS.touch(exist_ok=True)
    try:
        client.load_host_keys(str(_SSH_KNOWN_HOSTS))
    except Exception:
        # An empty/missing known_hosts file is expected on first run --
        # paramiko tolerates that fine. But if the file EXISTS with real
        # content and still failed to load (corrupt/unreadable), that's
        # silently discarding a real TOFU pin and re-accepting whatever
        # host key shows up next as new -- worth a log line so that's
        # distinguishable from a genuine first run.
        if _SSH_KNOWN_HOSTS.stat().st_size > 0:
            logger.warning("Could not load %s (exists but unreadable) -- treating as first-use", _SSH_KNOWN_HOSTS)
    # TOFU (trust-on-first-use), NOT the same as disabling host-key checking.
    # AutoAddPolicy accepts an unknown host key on first connect and we then
    # persist it via save_host_keys() below -- every SUBSEQUENT connection is
    # checked against that persisted key, and a key that changes out from under
    # us (a real MITM risk) still raises. Do NOT "simplify" this into
    # StrictHostKeyChecking=no / paramiko.WarningPolicy -- that would silently
    # accept ANY host key on EVERY connection, forever, with no persistence and
    # no detection of a changed key.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        client.connect(
            host,
            port=port,
            username=user,
            pkey=key,
            look_for_keys=False,
            allow_agent=False,
            timeout=10,
            banner_timeout=10,
            auth_timeout=10,
        )
        # Persist whatever host key TOFU just learned (a no-op on subsequent
        # connections where the key is already known and unchanged) so the
        # first-run capture actually survives past this one connection.
        client.save_host_keys(str(_SSH_KNOWN_HOSTS))

        stdin, stdout, stderr = client.exec_command(
            _PRUNE_SENTINEL, timeout=settings.unraid_ssh_prune_timeout_s
        )
        # Read BOTH streams to EOF before calling recv_exit_status() -- paramiko's
        # own docs warn that calling recv_exit_status() first can hang forever if
        # the remote's output is larger than the channel window, since nothing is
        # ever draining it to free that window. A single bounded .read(65536) call
        # doesn't fully drain a larger stream either (it returns early, leaving the
        # rest un-drained) -- these loops keep reading to genuine EOF, only capping
        # what's actually kept in memory. In practice `docker image prune -f`'s
        # stdout/stderr are tiny, so reading them sequentially (not concurrently)
        # carries negligible deadlock risk here -- not a general-purpose SSH client.
        out = _drain(stdout)
        err = _drain(stderr)
        exit_status = stdout.channel.recv_exit_status()
    finally:
        client.close()

    return exit_status, out, err


async def prune_docker_images() -> dict:
    """Prune DANGLING Docker images on Unraid over SSH -- deliberately NOT a
    full system-wide prune (see the module-section docstring above for why).

    Runs the blocking paramiko call in a thread (asyncio.to_thread) since
    paramiko has no async API. Returns:
      {"success": True, "reclaimed": "Total reclaimed space: ...", "removed_count": N}

    Raises RuntimeError on any failure -- a non-zero remote exit status, or any
    SSH-layer exception (auth failure, connection drop, timeout, bad host key).
    The "not configured" RuntimeErrors from _ssh_prune_sync propagate as-is
    (never double-wrapped); any other exception is re-raised with context.

    Does NOT call fetch.invalidate() -- pruning dangling images doesn't change
    any field fetch() returns (docker_containers only lists live containers,
    not dangling/untagged images).
    """
    try:
        exit_status, out, err = await asyncio.to_thread(_ssh_prune_sync)
    except RuntimeError as e:
        if "not configured" in str(e):
            raise
        raise RuntimeError(f"docker prune via SSH failed: {e}")
    except Exception as e:
        raise RuntimeError(f"docker prune via SSH failed: {e}")

    if exit_status != 0:
        raise RuntimeError(f"docker prune failed (exit {exit_status}): {err.strip()[:500]}")

    reclaimed = "Total reclaimed space: 0B"
    removed_count = 0
    for line in out.splitlines():
        if "Total reclaimed space:" in line:
            reclaimed = line.strip()[:200]
        elif line.startswith("Deleted:") or line.startswith("Untagged:"):
            removed_count += 1

    return {"success": True, "reclaimed": reclaimed, "removed_count": removed_count}
