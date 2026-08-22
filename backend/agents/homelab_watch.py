"""NEXUS-native homelab alert watcher — a 60s edge-alert loop over NEXUS's own
integrations. Covers: VM/LXC stopped, Docker container stopped, Unraid array
unhealthy, disk temp over threshold, garage door left open, a failed
vzdump backup, and (check_expected_resources, 2026-08-21) any live Docker/
VM/LXC state that deviates from a DECLARED baseline (backend/agents/
expected_resources.py) -- unlike the transition-triggered checks above, this
one catches a resource that was already in the wrong state before NEXUS ever
booted, which the others structurally cannot.

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
import asyncio
import html
import json
import logging
import time
from datetime import datetime

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

# Event-driven proposing (2026-08-17): a fired alert triggers one proposer tick
# instead of waiting up to 6h for the next scheduled one. In-memory, same
# accepted-tradeoff pattern as _active_alerts above.
_EVENT_PROPOSE_COOLDOWN_S = 1800  # ponytail: fixed 30min; make a Settings field if Brian wants tuning
_last_event_propose_at: datetime | None = None
_event_propose_task: asyncio.Task | None = None


def reset() -> None:
    """Clear all watcher state. Test hook — call at the start of each test."""
    global _garage_open_since, _last_event_propose_at, _event_propose_task
    _vm_states.clear()
    _docker_states.clear()
    _active_alerts.clear()
    _paged_alerts.clear()
    _garage_open_since = None
    _last_event_propose_at = None
    _event_propose_task = None


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


async def _edge_alert(key: str, active: bool, message: str, *, kind: str, detail: str | None = None) -> bool:
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

    `detail` is passed through to record_flag_ex verbatim -- optional,
    used by check_expected_resources to carry the {kind, identifier,
    observed_state} JSON that outcomes.resolve_flag reads back to sync the
    ExpectedResource baseline when a human resolves the flag.
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
    d = await outcomes.record_flag_ex("homelab_watch", key, message, severity=severity, detail=detail)
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


async def check_expected_resources() -> list[str]:
    """Diffs live Docker/VM/LXC state against the declared ExpectedResource
    baseline (backend/agents/expected_resources.py) -- closes the structural
    blind spot check_docker/check_proxmox_vms above can't: those only fire on
    a TRANSITION observed after NEXUS was already running, so anything that
    was already stopped (or already running when it shouldn't be) before
    NEXUS booted stays invisible to them forever. This check re-derives the
    mismatch from scratch every tick regardless of when it started -- exactly
    the "Only N Docker containers running -- verify this is intentional"
    briefing nag this feature exists to close.

    A resource with no declared row is not evaluated at all -- declaring a
    baseline is opt-in (via the REST endpoint or expected_resources.seed_from_live()),
    so an undeclared, intentionally-stopped Unraid container never generates
    noise merely by existing. A resource whose live state couldn't be read
    this tick (fetch failure) is skipped, not treated as a mismatch or a
    clear.
    """
    from backend.agents import expected_resources
    rows = await expected_resources.list_expected()
    if not rows:
        return []

    live_docker: dict[str, str] | None = None
    live_vm: dict[str, str] | None = None
    fired: list[str] = []

    for row in rows:
        kind, identifier, desired = row["kind"], row["identifier"], row["desired_state"]
        if kind == "docker":
            if live_docker is None:
                try:
                    from backend.integrations import unraid
                    data = await unraid.fetch()
                    live_docker = {
                        c["name"]: ("running" if (c.get("state") or "").upper() == "RUNNING" else "stopped")
                        for c in data.docker_containers if c.get("name")
                    }
                except Exception as e:
                    logger.warning(f"check_expected_resources: unraid fetch failed (ignored): {e}")
                    live_docker = {}
            observed = live_docker.get(identifier)
        else:  # vm | lxc
            if live_vm is None:
                try:
                    from backend.integrations import proxmox
                    pdata = await proxmox.fetch()
                    live_vm = {
                        f"{('lxc' if v.get('type') == 'lxc' else 'vm')}:{v.get('vmid')}":
                            ("running" if v.get("status") == "running" else "stopped")
                        for v in pdata.vms
                    }
                except Exception as e:
                    logger.warning(f"check_expected_resources: proxmox fetch failed (ignored): {e}")
                    live_vm = {}
            observed = live_vm.get(f"{kind}:{identifier}")

        if observed is None:
            continue  # couldn't read live state this tick -- not a deviation, not a clear

        key = f"expected:{kind}:{identifier}"
        detail = json.dumps({"kind": kind, "identifier": identifier, "observed_state": observed})
        message = (
            f"NEXUS: {kind} '{html.escape(identifier)}' is {observed} "
            f"but expected {desired} (declared baseline)."
        )
        did_fire = await _edge_alert(
            key, observed != desired, message, kind="homelab_expected_mismatch", detail=detail,
        )
        if did_fire:
            fired.append(key)
    return fired


async def _run_check(name: str, coro) -> list[str]:
    """Runs one check in isolation -- a bug/exception in one check (e.g. a
    malformed field from an integration) must not cancel the other five."""
    try:
        return await coro
    except Exception as exc:
        logger.error(f"homelab_watch check '{name}' error (ignored): {exc}")
        return []


def _maybe_propose_on_alert(fired_any: bool) -> None:
    """Kick one fire-and-forget propose_goals_tick when an alert fired this tick.

    Reacts to a homelab alert in ~seconds instead of waiting up to 6h for the
    next scheduled proposer tick. Bounded by a 30min cooldown here plus the
    proposer's own fingerprint debounce (6h) and TTL -- a burst of alerts in
    one tick still only triggers one run, since they all land in the same
    tick's fired lists anyway.
    """
    global _last_event_propose_at, _event_propose_task
    if not fired_any:
        return
    from backend.config import get_settings
    if not getattr(get_settings(), "proposer_enabled", True):
        return
    now = datetime.utcnow()
    if _last_event_propose_at and (now - _last_event_propose_at).total_seconds() < _EVENT_PROPOSE_COOLDOWN_S:
        return
    if _event_propose_task and not _event_propose_task.done():
        return
    _last_event_propose_at = now

    async def _run():
        try:
            from backend.agents.proposer import propose_goals_tick
            await propose_goals_tick()
        except Exception as e:
            logger.warning(f"event-driven proposer tick failed (ignored): {e}")

    # asyncio.create_task alone holds only a weak ref (same reasoning as
    # telegram_poller._message_tasks) -- the module-level var is what keeps
    # this alive, and also doubles as the already-running guard above.
    _event_propose_task = asyncio.create_task(_run())


async def run_homelab_watch() -> dict:
    """Top-level entry point called by the scheduler every 60 seconds.

    Gated by settings.homelab_watch_enabled. Runs all seven checks and
    returns a summary dict. NEVER raises — any exception is caught and
    logged."""
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "homelab_watch_enabled", True):
            return {"skipped": True}

        result = {
            "vms": await _run_check("vms", check_proxmox_vms()),
            "docker": await _run_check("docker", check_docker()),
            "array": await _run_check("array", check_unraid_array()),
            "disk_temps": await _run_check("disk_temps", check_disk_temps()),
            "garage": await _run_check("garage", check_garage()),
            "backups": await _run_check("backups", check_backups()),
            "expected": await _run_check("expected", check_expected_resources()),
        }
        _maybe_propose_on_alert(any(result.values()))
        try:
            from backend.agents import incident_diag
            incident_diag.maybe_diagnose(result)
        except Exception as e:
            logger.warning(f"incident_diag hookup failed (ignored): {e}")
        return result
    except Exception as exc:
        logger.error(f"run_homelab_watch error (ignored): {exc}")
        return {
            "vms": [], "docker": [], "array": [], "disk_temps": [], "garage": [],
            "backups": [], "expected": [],
        }
