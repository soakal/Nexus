"""NEXUS-native homelab alert watcher — a 60s edge-alert loop over NEXUS's own
integrations. Covers: VM/LXC stopped, Docker container stopped, Unraid array
unhealthy, disk temp over threshold, garage door left open, and a failed
vzdump backup.

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

Latches on ATTEMPT, not confirmed delivery — blocking on delivery confirmation
would need its own retry path. NEXUS's notify_phone already hands off to the
PendingDelivery retry queue + dead-letter watchdog, so blocking a scheduler
tick on Telegram delivery would be strictly worse. A muted kind still
latches — muting must not replay a stale alert.
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
# B10: keys whose alert actually paged (notify_phone fired, not suppressed by
# calibration/dedup) and hasn't recovered yet. A recovery notice only fires
# for a key in this set — an alert that was suppressed on the way up must not
# generate an unprompted "all clear" on the way down.
_paged_alerts: set[str] = set()
# Garage-open timer (time.monotonic() of when it was first observed open).
_garage_open_since: float | None = None


def reset() -> None:
    """Clear all watcher state. Test hook — call at the start of each test."""
    global _garage_open_since
    _vm_states.clear()
    _docker_states.clear()
    _active_alerts.clear()
    _paged_alerts.clear()
    _garage_open_since = None


async def _maybe_notify_recovery(key: str, message: str) -> None:
    """B10: fire an opt-in 'all clear' notice for a key that actually paged.
    Always clears the paged-tracking for `key` regardless of the flag — a
    disabled flag must not leak entries into `_paged_alerts` forever."""
    was_paged = key in _paged_alerts
    _paged_alerts.discard(key)
    if not was_paged:
        return
    try:
        from backend.config import get_settings
        if not get_settings().homelab_recovery_notify_enabled:
            return
        from backend import events
        await events.notify_phone(message, kind="homelab_recovered")
    except Exception as e:
        logger.warning(f"_maybe_notify_recovery failed for {key!r} (ignored): {e}")


async def _clear_flag_safe(key: str) -> None:
    """outcomes.clear_flag wrapped so a DB hiccup can never surface as an
    unhandled exception out of a check function -- record_flag already
    guarantees NEVER raises itself (backend/agents/outcomes.py); clear_flag
    does not carry that same top-level guard, so it lives here instead."""
    from backend.agents import outcomes
    try:
        await outcomes.clear_flag("homelab_watch", key)
    except Exception as e:
        logger.warning(f"clear_flag failed for {key!r} (ignored): {e}")


async def _edge_alert(key: str, active: bool, message: str, *, kind: str) -> bool:
    """Level-triggered latch: fires once when `active` transitions True, clears
    when it goes False. Returns whether it fired THIS call.

    Every falling-edge tick (active=False) unconditionally auto-resolves any
    open flag for this fingerprint (spec docs/outcome-tracker-spec.md §2.2-A)
    before the in-memory latch is touched -- a garage that got closed clears
    its own flag. A rising edge that newly latches records a flag first via
    record_flag_ex; when the write/page gate says to surface it, notify_phone
    fires -- with two Telegram buttons if an id came back, or buttons=None if
    it didn't (e.g. outcome_flags_enabled=False fails open with id=None,
    surface=True, matching pre-outcome-tracker behavior). When the gate says
    not to surface (calibration suppression or dedup), notify_phone is
    skipped entirely -- the latch still fires (return value below tracks
    that, not paging) so re-alert suppression stays correct.
    """
    if not active:
        await _clear_flag_safe(key)
        _active_alerts.discard(key)
        await _maybe_notify_recovery(key, f"NEXUS: recovered — {key} is back to normal.")
        return False
    if key in _active_alerts:
        return False
    _active_alerts.add(key)
    from backend.agents import outcomes
    severity = "high" if key in ("unraid_array", "vzdump_failed") else "medium"
    d = await outcomes.record_flag_ex("homelab_watch", key, message, severity=severity)
    if d["surface"]:
        buttons = None
        if d["id"] is not None:
            buttons = [
                {"text": "✓ Resolved", "callback_data": f"flag:resolved:{d['id']}"},
                {"text": "✗ False alarm", "callback_data": f"flag:false_positive:{d['id']}"},
            ]
        from backend import events
        await events.notify_phone(message, kind=kind, buttons=buttons)
        _paged_alerts.add(key)
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
            from backend.agents import outcomes
            d = await outcomes.record_flag_ex(
                "homelab_watch", f"vm:{vmid}",
                f"VM/LXC '{name}' (id {vmid}) stopped (was running).",
                severity="high",
            )
            if d["surface"]:
                from backend import events
                await events.notify_phone(
                    f"NEXUS: VM/LXC '{name}' (id {vmid}) stopped (was running).",
                    kind="homelab_vm_stopped",
                    buttons=[{"text": "▶ Start", "callback_data": f"vm:start:{vmid}"}],
                )
                _paged_alerts.add(f"vm:{vmid}")
            fired.append(f"vm:{vmid}")
        elif prev != "running" and status == "running":
            await _clear_flag_safe(f"vm:{vmid}")
            recovered_name = html.escape(next((vm.get("name") or "" for vm in data.vms if str(vm["vmid"]) == vmid), vmid))
            await _maybe_notify_recovery(
                f"vm:{vmid}",
                f"NEXUS: VM/LXC '{recovered_name}' (id {vmid}) is running again.",
            )
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
            from backend.agents import outcomes
            d = await outcomes.record_flag_ex(
                "homelab_watch", f"docker:{name}",
                f"Docker container '{html.escape(name)}' stopped.",
                severity="medium",
            )
            if d["surface"]:
                buttons = None
                if unraid._SAFE_CONTAINER_ID.match(name) and len(f"docker:restart:{name}".encode()) <= 64:
                    buttons = [{"text": "↺ Restart", "callback_data": f"docker:restart:{name}"}]
                from backend import events
                await events.notify_phone(
                    f"NEXUS: Docker container '{html.escape(name)}' stopped.",
                    kind="homelab_docker_stopped",
                    buttons=buttons,
                )
                _paged_alerts.add(f"docker:{name}")
            fired.append(f"docker:{name}")
        elif prev != "RUNNING" and state == "RUNNING":
            await _clear_flag_safe(f"docker:{name}")
            await _maybe_notify_recovery(
                f"docker:{name}",
                f"NEXUS: Docker container '{html.escape(name)}' is running again.",
            )
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
