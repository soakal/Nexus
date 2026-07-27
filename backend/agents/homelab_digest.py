"""NEXUS-native homelab daily digest (Phase 3 of the Hermes decoupling).

Ports Hermes's main.py:daily_digest() 8am cron onto NEXUS's own integrations,
so this proactive homelab-status report survives Hermes's bot process ever
being stopped. Distinct from homelab_watch.py, which is edge-triggered
(alerts on problems only) -- this one always sends, once a day.

Deliberately NOT ported: weather and calendar (backend/agents/briefing.py's
morning_briefing already covers both, 5 minutes earlier -- this job is
scheduled at briefing_time+5) and Jellyfin now-playing (Hermes itself only
shows that section when something IS playing, which at 8am is effectively
never, and NEXUS has no native Jellyfin integration -- only the kept Hermes
action-relay bridge).
"""
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """briefing_timezone-aware now, not the host process's local time -- same
    convention as backend/integrations/calendar.py (a prior port bug fixed
    there for exactly this reason)."""
    from backend.config import get_settings
    try:
        tz = ZoneInfo(get_settings().briefing_timezone)
    except Exception:
        tz = None
    return datetime.now(tz)

# Mirrors hermes-agent/main.py's _HA_IGNORE_PREFIXES/_HA_IGNORE_SUBSTRINGS --
# entities that are chronically unavailable and not actionable, so the daily
# unavailable-count doesn't drown in noise from iPads/Alexas/identify buttons.
_HA_IGNORE_PREFIXES = (
    "button.",
    "input_button.",
    "sensor.ipad_",
    "binary_sensor.ipad_",
    "notify.ipad",
    "stt.",
    "tts.",
    "sensor.homepage_",
    "binary_sensor.pve_homepage",
)
_HA_IGNORE_SUBSTRINGS = (
    "_identify", "christmas", "iphone", "ipad",
    "alexa", "echo", "firestick", "fire_stick", "fire_tv",
    "_speak", "_announce",
)


def _count_unavailable(ha) -> int:
    count = 0
    for e in getattr(ha, "entities", []) or []:
        if not isinstance(e, dict) or e.get("state") not in ("unavailable", "unknown"):
            continue
        eid = e.get("entity_id", "")
        if any(eid.startswith(p) for p in _HA_IGNORE_PREFIXES):
            continue
        if any(s in eid for s in _HA_IGNORE_SUBSTRINGS):
            continue
        count += 1
    return count


async def _section_proxmox() -> str:
    from backend.integrations import proxmox
    data = await proxmox.fetch()
    lines = [
        f"node: {html.escape(data.node)}",
        f"status: {html.escape(data.node_status)}",
        f"cpu: {data.cpu_pct}%",
        f"memory: {data.mem_used_gb}/{data.mem_total_gb} GB",
        f"storage: {data.storage_used_gb}/{data.storage_total_gb} GB",
    ]
    vm_lines = [
        f"{'🟢' if vm.get('status') == 'running' else '🔴'} "
        f"{'VM' if vm.get('type') == 'qemu' else 'LXC'} {vm.get('vmid')}: "
        f"{html.escape(str(vm.get('name') or ''))} ({html.escape(str(vm.get('status')))})"
        for vm in data.vms
    ]
    return "\n".join(lines) + "\n\nVMs\n" + ("\n".join(vm_lines) if vm_lines else "(none)")


async def _section_unifi() -> str:
    # Deliberately no "new devices" line: UniFiData.new_devices is one-shot
    # (unifi.py inserts a KnownDevice row the first time it sees a MAC), and
    # by the time this job runs at briefing_time+5 the morning briefing plus
    # every other continuous caller has already drained it -- it would read
    # empty every single day, not just when nothing's actually new.
    from backend.integrations import unifi
    data = await unifi.fetch()
    return f"{data.client_count} clients, uplink {html.escape(data.uplink_status)}"


async def _section_unraid() -> str:
    from backend.integrations import unraid
    data = await unraid.fetch()
    lines = [
        f"array: {html.escape(data.array_status)} (parity: {html.escape(data.parity_status)})",
        f"storage: {data.storage_used_gb}/{data.storage_total_gb} GB",
    ]
    disk_lines = [
        f"{html.escape(d['name'])}: {d['temp']}C"
        if isinstance(d.get("temp"), (int, float))
        else f"{html.escape(d['name'])}: spun down"
        for d in data.disk_health
    ]
    docker_lines = [
        f"{html.escape(c.get('name', ''))}: {html.escape(c.get('status', ''))}"
        for c in data.docker_containers
    ]
    return (
        "\n".join(lines)
        + "\n\nDisk health\n" + ("\n".join(disk_lines) if disk_lines else "(none)")
        + "\n\nDocker\n" + ("\n".join(docker_lines) if docker_lines else "(none)")
    )


async def _section_adguard() -> str:
    from backend.integrations import adguard
    data = await adguard.fetch()
    state = "on" if data.filtering_enabled else "off" if data.filtering_enabled is False else "unknown"
    return f"{data.blocked_today}/{data.queries_today} blocked ({data.blocked_pct}%), filtering {state}"


def _count_or_plus(n: int, cap: int) -> str:
    """channels_dvr.fetch() slices upcoming/failed_recordings to `cap` entries
    -- a bare len() at the cap reads as an exact total when it's really a
    truncation ("upcoming: 10" whether there are 10 or 200 scheduled)."""
    return f"{n}+" if n >= cap else str(n)


async def _section_channels_dvr() -> str:
    from backend.integrations import channels_dvr
    data = await channels_dvr.fetch()
    lines = [
        f"recording now: {len(data.recording_now)}",
        f"upcoming: {_count_or_plus(len(data.upcoming), 10)}",
        f"storage: {data.storage_used_gb}/{data.storage_total_gb} GB",
    ]
    if data.failed_recordings:
        lines.append(f"failed (24h): {_count_or_plus(len(data.failed_recordings), 10)}")
    return "\n".join(lines)


async def _section_ha() -> str:
    from backend.agents.chat import extract_temperature_sensors
    from backend.config import get_settings
    from backend.integrations import homeassistant
    settings = get_settings()
    data = await homeassistant.fetch()

    entity = next(
        (e for e in data.entities if e.get("entity_id") == settings.homelab_garage_entity_id),
        None,
    )
    garage = html.escape((entity or {}).get("state", "unknown"))

    temps = extract_temperature_sensors(data)
    temps_line = (
        " | ".join(f"{html.escape(t['label'])} {t['value_f']:.0f}F" for t in temps)
        if temps else "(none)"
    )

    unavail = _count_unavailable(data)
    ha_line = f"{unavail} entities unavailable" if unavail else "all entities OK"

    return f"Garage: {garage}\nTemps: {temps_line}\nHA: {ha_line}"


async def _section_sports() -> str:
    # Escaped even though no real MLB/NFL team name has ever contained HTML
    # metacharacters -- this is the one section whose text comes from a
    # third party (MLB Stats API / ESPN), not from Brian's own homelab.
    from backend.integrations import sports
    tigers = await sports.get_tigers_last_game()
    lions = await sports.get_lions_last_game()
    parts = []
    if tigers:
        parts.append(f"Tigers: {html.escape(tigers)}")
    if lions:
        parts.append(f"Lions: {html.escape(lions)}")
    return "\n".join(parts) if parts else "(no games)"


async def _run_section(name: str, coro) -> str:
    """Runs one section in isolation -- a bug/exception in one integration must
    not cancel the other sections (mirrors homelab_watch.py's _run_check)."""
    try:
        return await coro
    except Exception as exc:
        logger.warning(f"homelab_digest section '{name}' error (degraded): {exc}")
        return "error: unavailable"


def _format_now() -> str:
    """Windows-safe equivalent of Hermes's '%-d'/'%-I' (glibc-only, crashes on
    Windows) -- plain ints instead of the strftime no-pad flag."""
    now = _now()
    return f"{now:%A, %B} {now.day} {now:%Y} {now.hour % 12 or 12}:{now:%M %p}"


async def _section_spend() -> str:
    """Isolated the same as every other section -- a DB hiccup here must
    degrade to 'unknown', not take down the whole digest (every other field
    already comes back fine by the time this runs)."""
    import asyncio
    from backend.safety import governor
    spend = await asyncio.to_thread(governor.today_spend_usd)
    return f"${spend:.4f}"


async def build_digest_text() -> str:
    sections = {
        "ha": await _run_section("ha", _section_ha()),
        "sports": await _run_section("sports", _section_sports()),
        "proxmox": await _run_section("proxmox", _section_proxmox()),
        "unifi": await _run_section("unifi", _section_unifi()),
        "unraid": await _run_section("unraid", _section_unraid()),
        "adguard": await _run_section("adguard", _section_adguard()),
        "channels_dvr": await _run_section("channels_dvr", _section_channels_dvr()),
        "spend": await _run_section("spend", _section_spend()),
    }

    return (
        f"NEXUS homelab digest — {_format_now()}\n\n"
        f"{sections['ha']}\n\n"
        f"Sports\n{sections['sports']}\n\n"
        f"Proxmox\n{sections['proxmox']}\n\n"
        f"Unifi\n{sections['unifi']}\n\n"
        f"Unraid\n{sections['unraid']}\n\n"
        f"AdGuard\n{sections['adguard']}\n\n"
        f"Channels DVR\n{sections['channels_dvr']}\n\n"
        f"API spend today: {sections['spend']}"
    )


async def run_homelab_digest() -> dict:
    """Top-level entry point called by the scheduler once a day.

    Gated by settings.homelab_digest_enabled. NEVER raises -- any exception is
    caught and logged, matching homelab_watch.py's discipline."""
    try:
        from backend.config import get_settings
        if not getattr(get_settings(), "homelab_digest_enabled", True):
            return {"skipped": True}

        text = await build_digest_text()

        from backend import events
        delivered = await events.notify_phone(text, kind="homelab_digest")

        from backend.integrations import obsidian
        now = _now().strftime("%Y-%m-%d %H:%M")
        await obsidian.emit_event("nexus.daily-digest", f"Daily digest — {now}", text)

        return {"delivered": delivered}
    except Exception as exc:
        logger.error(f"run_homelab_digest error (ignored): {exc}")
        return {"delivered": False}
