"""NEXUS-native homelab alert watcher (Phase 2c of the Hermes decoupling).

Ports Hermes's watcher.py 60s edge-alert loop onto NEXUS's own integrations, so
these pages keep firing even if Hermes's bot process is ever stopped. Covers:
VM/LXC stopped, Docker container stopped, Unraid array unhealthy, disk temp
over threshold, garage door left open, and a failed vzdump backup.

Deliberately NOT built here: doorbell/camera alerts (declined by Brian — needs
a separate 5s poll + send_photo, not worth it yet), and NEXUS's own liveness
check (a process cannot monitor its own death — that needs EXTERNAL monitoring
such as Uptime-Kuma or a cron job elsewhere; until that exists, nothing in
NEXUS covers "NEXUS itself is down").

Edge-trigger discipline (mirrors backend/agents/watchdog.py's check_* shape):
fire once on a transition into "bad", stay silent while it remains bad, re-arm
only once it clears. State is process-local/in-memory (not DB-persisted) — its
entire useful lifetime is the 60s until the next tick, so a restart losing it
is an acceptable, cheap tradeoff (see reset()).

Latches on ATTEMPT, not confirmed delivery — unlike Hermes's watcher, which
blocked on delivery confirmation because it had no retry path. NEXUS's
notify_phone already hands off to the PendingDelivery retry queue + dead-letter
watchdog, so blocking a scheduler tick on Telegram delivery would be strictly
worse. A muted kind still latches — muting must not replay a stale alert.
"""
import html
import logging
import time

logger = logging.getLogger(__name__)

# Transition-triggered state: previous observed status per object.
_vm_states: dict[str, str] = {}
_docker_states: dict[str, str] = {}
# Level-triggered state: currently-active alert keys.
_active_alerts: set[str] = set()
# Garage-open timer (time.monotonic() of when it was first observed open).
_garage_open_since: float | None = None


def reset() -> None:
    """Clear all watcher state. Test hook — call at the start of each test."""
    global _garage_open_since
    _vm_states.clear()
    _docker_states.clear()
    _active_alerts.clear()
    _garage_open_since = None


async def _edge_alert(key: str, active: bool, message: str, *, kind: str) -> bool:
    """Level-triggered latch: fires once when `active` transitions True, clears
    when it goes False. Returns whether it fired THIS call."""
    if not active:
        _active_alerts.discard(key)
        return False
    if key in _active_alerts:
        return False
    _active_alerts.add(key)
    from backend import events
    await events.notify_phone(message, kind=kind)
    return True


async def check_proxmox_vms() -> list[str]:
    """VM/LXC running -> not-running. Only alerts when a PRIOR 'running'
    observation exists, so a VM already stopped on the first tick (or one
    Brian intentionally stops) never pages on discovery — only on transition."""
    fired: list[str] = []
    try:
        from backend.integrations import proxmox
        data = await proxmox.fetch()
    except Exception as e:
        logger.warning(f"check_proxmox_vms: fetch failed (ignored): {e}")
        return fired

    current = {str(vm["vmid"]): vm.get("status", "unknown") for vm in data.vms}
    for vmid, status in current.items():
        prev = _vm_states.get(vmid)
        if prev == "running" and status != "running":
            name = html.escape(next((vm.get("name") or "" for vm in data.vms if str(vm["vmid"]) == vmid), vmid))
            from backend import events
            await events.notify_phone(
                f"NEXUS: VM/LXC '{name}' (id {vmid}) stopped (was running).",
                kind="homelab_vm_stopped",
                buttons=[{"text": "▶ Start", "callback_data": f"vm:start:{vmid}"}],
            )
            fired.append(f"vm:{vmid}")
    _vm_states.clear()
    _vm_states.update(current)
    return fired


async def check_docker() -> list[str]:
    """Docker container RUNNING -> not-RUNNING, same discovery-safe transition
    rule as check_proxmox_vms."""
    fired: list[str] = []
    try:
        from backend.integrations import unraid
        data = await unraid.fetch()
    except Exception as e:
        logger.warning(f"check_docker: fetch failed (ignored): {e}")
        return fired

    current = {c["name"]: (c.get("state") or "").upper() for c in data.docker_containers if c.get("name")}
    for name, state in current.items():
        prev = _docker_states.get(name)
        if prev == "RUNNING" and state != "RUNNING":
            buttons = None
            if unraid._SAFE_CONTAINER_ID.match(name) and len(f"docker:restart:{name}".encode()) <= 64:
                buttons = [{"text": "↺ Restart", "callback_data": f"docker:restart:{name}"}]
            from backend import events
            await events.notify_phone(
                f"NEXUS: Docker container '{html.escape(name)}' stopped.",
                kind="homelab_docker_stopped",
                buttons=buttons,
            )
            fired.append(f"docker:{name}")
    _docker_states.clear()
    _docker_states.update(current)
    return fired


async def check_unraid_array() -> list[str]:
    """Array not 'started'. 'unknown' (a read problem, not a breach — owned by
    the uptime job / contract canary) is deliberately NOT treated as bad."""
    try:
        from backend.integrations import unraid
        data = await unraid.fetch()
    except Exception as e:
        logger.warning(f"check_unraid_array: fetch failed (ignored): {e}")
        return []

    bad = data.array_status not in ("started", "unknown")
    fired = await _edge_alert(
        "unraid_array",
        bad,
        f"NEXUS: Unraid array status is '{data.array_status}' (expected 'started').",
        kind="homelab_array",
    )
    return ["unraid_array"] if fired else []


async def check_disk_temps() -> list[str]:
    """Any disk temp over the configured threshold. Non-numeric/missing temps
    (spun-down disks) are skipped, not coerced."""
    try:
        from backend.config import get_settings
        from backend.integrations import unraid
        data = await unraid.fetch()
    except Exception as e:
        logger.warning(f"check_disk_temps: fetch failed (ignored): {e}")
        return []

    threshold = get_settings().homelab_disk_temp_warn_c
    hot = [
        d for d in data.disk_health
        if isinstance(d.get("temp"), (int, float)) and d["temp"] > threshold
    ]
    detail = ", ".join(f"{html.escape(d['name'])}={d['temp']}C" for d in hot)
    fired = await _edge_alert(
        "unraid_temp",
        bool(hot),
        f"NEXUS: Unraid disk(s) over {threshold}C — {detail}.",
        kind="homelab_disk_temp",
    )
    return ["unraid_temp"] if fired else []


async def check_garage() -> list[str]:
    """Garage door open longer than the configured minutes. Entity missing
    from HA (never installed, HA down) degrades to 'closed' + timer cleared —
    no alert, no exception."""
    global _garage_open_since
    try:
        from backend.config import get_settings
        from backend.integrations import homeassistant
        settings = get_settings()
        data = await homeassistant.fetch()
    except Exception as e:
        logger.warning(f"check_garage: fetch failed (ignored): {e}")
        return []

    entity = next(
        (e for e in data.entities if e.get("entity_id") == settings.homelab_garage_entity_id),
        None,
    )
    is_open = bool(entity) and entity.get("state") == "open"

    if not is_open:
        _garage_open_since = None
        fired = await _edge_alert("garage_open", False, "", kind="homelab_garage")
        return ["garage_open"] if fired else []

    if _garage_open_since is None:
        _garage_open_since = time.monotonic()

    open_minutes = (time.monotonic() - _garage_open_since) / 60
    over_threshold = open_minutes >= settings.homelab_garage_open_minutes
    fired = await _edge_alert(
        "garage_open",
        over_threshold,
        f"NEXUS: garage door has been open for over {settings.homelab_garage_open_minutes} minutes.",
        kind="homelab_garage",
    )
    return ["garage_open"] if fired else []


async def check_backups() -> list[str]:
    """vzdump backup status == 'failed'. A raise (transport error) is an
    outage, not a failed backup — not treated as bad."""
    try:
        from backend.integrations import proxmox
        data = await proxmox.fetch_backups()
    except Exception as e:
        logger.warning(f"check_backups: fetch failed (ignored): {e}")
        return []

    bad = data.get("status") == "failed"
    fired = await _edge_alert(
        "vzdump_failed",
        bad,
        f"NEXUS: latest Proxmox backup failed ({html.escape(str(data.get('detail', '')))}).",
        kind="homelab_backup_failed",
    )
    return ["vzdump_failed"] if fired else []


async def _run_check(name: str, coro) -> list[str]:
    """Runs one check in isolation -- a bug/exception in one check (e.g. a
    malformed field from an integration) must not cancel the other five."""
    try:
        return await coro
    except Exception as exc:
        logger.error(f"homelab_watch check '{name}' error (ignored): {exc}")
        return []


async def run_homelab_watch() -> dict:
    """Top-level entry point called by the scheduler every 60 seconds.

    Gated by settings.homelab_watch_enabled. Runs all six checks and returns a
    summary dict. NEVER raises — any exception is caught and logged."""
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "homelab_watch_enabled", True):
            return {"skipped": True}

        return {
            "vms": await _run_check("vms", check_proxmox_vms()),
            "docker": await _run_check("docker", check_docker()),
            "array": await _run_check("array", check_unraid_array()),
            "disk_temps": await _run_check("disk_temps", check_disk_temps()),
            "garage": await _run_check("garage", check_garage()),
            "backups": await _run_check("backups", check_backups()),
        }
    except Exception as exc:
        logger.error(f"run_homelab_watch error (ignored): {exc}")
        return {"vms": [], "docker": [], "array": [], "disk_temps": [], "garage": [], "backups": []}
