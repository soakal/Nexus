import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
from sqlmodel import Session, select

from backend.cache import async_ttl_cache
from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)

# Entities that are chronically unavailable and not actionable, so the
# unavailable-duration tracker (HaEntityUnavailable) doesn't drown in noise
# from iPads/Alexas/identify buttons/HA Cloud STT-TTS. Moved here from
# backend/agents/homelab_digest.py (2026-08-2x) -- this module now owns
# "what counts as an actionable unavailable entity"; homelab_digest and
# briefing both consume the resulting unavailable_report()/
# format_unavailable_summary() instead of re-deriving their own count.
_UNAVAIL_IGNORE_PREFIXES = (
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
_UNAVAIL_IGNORE_SUBSTRINGS = (
    "_identify", "christmas", "iphone", "ipad",
    "alexa", "echo", "firestick", "fire_stick", "fire_tv",
    "_speak", "_announce",
)


def _is_ignorable_unavailable(entity_id: str) -> bool:
    return (
        any(entity_id.startswith(p) for p in _UNAVAIL_IGNORE_PREFIXES)
        or any(s in entity_id for s in _UNAVAIL_IGNORE_SUBSTRINGS)
    )


@dataclass
class HAData:
    entities: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    cloud_alerts: list = field(default_factory=list)
    last_updated: datetime = field(default_factory=datetime.utcnow)


class IntegrationError(Exception):
    pass


# ponytail: allowlist replaces the old denylist — only these entities ever appear in alerts
_ALERT_ALLOWLIST = frozenset({
    # Lights
    # tall_light_lr_christmas_tree_plug (light.* + switch.*) intentionally excluded
    # (Brian, 2026-07-14): an HA automation turns it off nightly at 11:59pm, so it
    # doesn't need to surface as a NEXUS alert either. Mirrors the proposer WATCH
    # exclusion — this is the SECOND surface, same as the porch-light two-layer fix.
    # light.left_porch_light / light.right_porch_light intentionally excluded:
    # fixtures are powered off due to water damage (Brian, 2026-07-11) — orphaned
    # from the ZHA/Zigbee mesh since 2026-07-08, expected "unavailable" until
    # physically repaired. Re-add once the fixtures are back in service.
    "light.left_garage_light",
    "light.right_garage_light",
    # Garage door
    "cover.garage_door_garage_door",
    # August / back door lock
    "lock.dining_room",
    # UniFi integration health
    "switch.unifi_network",
    # AdGuard
    "switch.adguard_home_protection",
    "switch.adguard_home_filtering",
    # Proxmox (PVE integration)
    "binary_sensor.pve_adguard",
})


@async_ttl_cache(30)
async def fetch() -> HAData:
    from backend.config import get_settings
    settings = get_settings()
    host = settings.hass_host
    try:
        token = settings.hass_token
    except Exception:
        raise IntegrationError("HASS_TOKEN not configured")

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # 10s not 5: /api/states returns ~1200 entities and intermittently exceeds
    # 5s, which surfaced as spurious "Home Assistant unreachable" 502s in the UI.
    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=10) as client:
        resp = await client.get(f"{host}/api/states", headers=headers)
        resp.raise_for_status()
        entities = resp.json()

        # Only alert on the explicit allowlist — everything else is noise.
        alerts = [
            e["entity_id"]
            for e in entities
            if e.get("state") in ("unavailable", "unknown")
            and e.get("entity_id") in _ALERT_ALLOWLIST
        ]

        # HA Cloud health checks: surface structured cloud alerts.
        cloud_alerts = []
        by_id = {e.get("entity_id"): e for e in entities if e.get("entity_id")}
        for cloud_id, alert_type in (
            ("stt.home_assistant_cloud", "cloud_stt"),
            ("tts.home_assistant_cloud", "cloud_tts"),
        ):
            ent = by_id.get(cloud_id)
            if ent and ent.get("state") == "unavailable":
                cloud_alerts.append({
                    "entity": cloud_id,
                    "type": alert_type,
                    "message": (
                        "HA Cloud STT unavailable — check Home Assistant Cloud "
                        "subscription at ha.io/cloud"
                        if alert_type == "cloud_stt"
                        else "HA Cloud TTS unavailable — check Home Assistant "
                        "Cloud subscription at ha.io/cloud"
                    ),
                    "state": ent.get("state"),
                })

        # If cloud is unavailable, attempt a best-effort reload. This must not
        # break the main fetch on failure.
        if cloud_alerts:
            try:
                await try_reload_cloud(host, headers, client)
            except Exception as e:
                logger.warning(f"HA Cloud reload attempt failed: {e}")

    await _track_unavailable(entities)

    return HAData(entities=entities, alerts=alerts, cloud_alerts=cloud_alerts)


def _db_track_unavailable(unavailable_ids: set, now: datetime) -> None:
    """Sync half — MUST only ever be called via asyncio.to_thread. Session
    work inside an `async def` blocks the whole event loop, and this runs on
    the fetch() hot path behind /api/health and every briefing gather."""
    from backend.database import HaEntityUnavailable, engine

    with Session(engine) as session:
        tracked = session.exec(select(HaEntityUnavailable)).all()
        tracked_ids = set()
        for row in tracked:
            tracked_ids.add(row.entity_id)
            if row.entity_id in unavailable_ids:
                row.last_checked_at = now
                session.add(row)
            else:
                session.delete(row)
        for new_id in unavailable_ids - tracked_ids:
            session.add(HaEntityUnavailable(
                entity_id=new_id, first_seen_unavailable_at=now, last_checked_at=now,
            ))
        session.commit()


async def _track_unavailable(entities: list) -> None:
    """Upsert per-entity unavailable duration into HaEntityUnavailable, once
    per real fetch() (the ~30s async_ttl_cache cadence -- see this module's
    top-level decorator). Newly-unavailable -> insert (first_seen starts
    now); recovered -> row deleted; still-unavailable -> only
    last_checked_at bumped, first_seen_unavailable_at is never touched
    (duration = now - first_seen_unavailable_at, that's the whole point).

    Best-effort, like obsidian.emit_event -- a DB hiccup here must never
    fail or slow down the main fetch() path it's called from. All DB work is
    off-loop via asyncio.to_thread.
    """
    try:
        now = datetime.utcnow()
        unavailable_ids = {
            e.get("entity_id")
            for e in entities
            if e.get("state") in ("unavailable", "unknown")
            and e.get("entity_id")
            and not _is_ignorable_unavailable(e.get("entity_id", ""))
        }
        await asyncio.to_thread(_db_track_unavailable, unavailable_ids, now)
    except Exception as e:
        logger.warning(f"_track_unavailable failed (ignored): {e}")


async def unavailable_report() -> dict:
    """Age-bucketed summary of HaEntityUnavailable -- the actionable
    replacement for a flat "N entities unavailable" count. `persistent`
    (>=7 days) and `recent` (<1h) are the two buckets briefing/
    homelab_digest surface explicitly; `total` excludes suppressed rows
    (they've been marked known-dead, stop counting) while `items` lists
    every tracked row, suppressed or not, so a triage UI can still show
    (and un-dismiss) a suppressed entity.

    Never raises -- degrades to an all-zero/empty report on any DB error,
    matching this module's general fetch() discipline.
    """
    from backend.database import HaEntityUnavailable, engine

    now = datetime.utcnow()

    def _read():
        with Session(engine) as session:
            # Materialise plain tuples inside the Session -- no ORM object may
            # cross the to_thread boundary.
            return [
                (r.entity_id, r.first_seen_unavailable_at, r.last_checked_at, r.suppressed)
                for r in session.exec(select(HaEntityUnavailable)).all()
            ]

    try:
        raw = await asyncio.to_thread(_read)
    except Exception as e:
        logger.warning(f"unavailable_report failed (ignored): {e}")
        return {"total": 0, "persistent": 0, "recent": 0, "items": []}

    rows = [
        SimpleNamespace(
            entity_id=eid, first_seen_unavailable_at=first,
            last_checked_at=last, suppressed=supp,
        )
        for eid, first, last, supp in raw
    ]

    active = [r for r in rows if not r.suppressed]
    persistent = sum(1 for r in active if (now - r.first_seen_unavailable_at) >= timedelta(days=7))
    recent = sum(1 for r in active if (now - r.first_seen_unavailable_at) < timedelta(hours=1))
    items = [
        {
            "entity_id": r.entity_id,
            "first_seen_unavailable_at": r.first_seen_unavailable_at.isoformat(),
            "last_checked_at": r.last_checked_at.isoformat(),
            "age_seconds": (now - r.first_seen_unavailable_at).total_seconds(),
            "suppressed": r.suppressed,
        }
        for r in sorted(rows, key=lambda r: r.first_seen_unavailable_at)
    ]
    return {"total": len(active), "persistent": persistent, "recent": recent, "items": items}


def format_unavailable_summary(report: dict) -> str:
    """Renders unavailable_report()'s output as one actionable line, e.g.
    "12 unavailable >7 days (persistently dead), 3 unavailable <1h (likely
    transient)" -- what homelab_digest's HA line and the briefing prompt
    both use instead of a bare entity count."""
    total = report.get("total", 0)
    if not total:
        return "all entities OK"
    persistent = report.get("persistent", 0)
    recent = report.get("recent", 0)
    middle = total - persistent - recent
    parts = []
    if persistent:
        parts.append(f"{persistent} unavailable >7 days (persistently dead)")
    if recent:
        parts.append(f"{recent} unavailable <1h (likely transient)")
    if middle > 0:
        parts.append(f"{middle} unavailable 1h-7d")
    return ", ".join(parts) if parts else f"{total} unavailable"


async def set_unavailable_suppressed(entity_id: str, suppressed: bool) -> bool:
    """Marks (or unmarks) a tracked-unavailable entity "known-dead, stop
    counting" -- the triage list's dismiss action. Returns False if the
    entity isn't currently tracked (already recovered, or never was
    unavailable), which the API route turns into a 404."""
    from backend.database import HaEntityUnavailable, engine

    def _write() -> bool:
        with Session(engine) as session:
            row = session.exec(
                select(HaEntityUnavailable).where(HaEntityUnavailable.entity_id == entity_id)
            ).first()
            if not row:
                return False
            row.suppressed = suppressed
            session.add(row)
            session.commit()
        return True

    return await asyncio.to_thread(_write)


async def try_reload_cloud(host: str, headers: dict, client: httpx.AsyncClient) -> dict | None:
    """Best-effort reload of the HA Cloud integration via a service call.

    Wrapped by callers in try/except — logs the result and never raises out of
    the main fetch path on its own (errors are logged here too).
    """
    url = f"{host}/api/services/homeassistant/reload_config_entry"
    try:
        resp = await client.post(url, json={"entry_id": "cloud"}, headers=headers)
        resp.raise_for_status()
        result = resp.json() if resp.content else {}
        logger.info("HA Cloud reload_config_entry succeeded")
        return result
    except Exception as e:
        logger.warning(f"HA Cloud reload_config_entry failed: {e}")
        raise


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        headers = {"Authorization": f"Bearer {settings.hass_token}"}
        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=5) as client:
            resp = await client.get(f"{settings.hass_host}/api/", headers=headers)
            return resp.status_code == 200
    except Exception:
        return False


async def call_service(domain: str, service: str, data: dict | None = None) -> dict:
    from backend.config import get_settings
    settings = get_settings()

    # HA returns 200 with an empty changed-entities list for BOTH a nonexistent
    # entity_id AND a valid entity already in the target state — the response
    # alone can't tell those apart. Validate existence up front (via the cached
    # entity list) so a typo'd/hallucinated entity_id raises loudly instead of
    # the caller reading empty-success as "done".
    raw_target = (data or {}).get("entity_id")
    if raw_target:
        targets = [raw_target] if isinstance(raw_target, str) else list(raw_target)
        known = {e.get("entity_id") for e in (await fetch()).entities}
        unknown = [t for t in targets if t not in known]
        if unknown:
            raise IntegrationError(f"unknown entity_id(s), not found in HA: {unknown}")

    headers = {"Authorization": f"Bearer {settings.hass_token}", "Content-Type": "application/json"}
    url = f"{settings.hass_host}/api/services/{domain}/{service}"
    # 15s not 5: HomeKit-bridged devices (Ecobee) and the Konnected garage door
    # can take >5s to ack a service call — 5s made mode changes 502 spuriously.
    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=15) as client:
        resp = await client.post(url, json=data or {}, headers=headers)
        resp.raise_for_status()
        result = resp.json()
    # The cached entity snapshot is now stale (we just changed a device); drop it
    # so the frontend's post-toggle reload sees the real new state, not the cache.
    fetch.invalidate()
    return result


def _extract_entity_ids(obj) -> set[str]:
    """Recursively pull every `entity_id` value out of an HA automation config.

    Automation configs nest entity_id references all over the place — trigger
    `entity_id`, condition `entity_id`, action `target.entity_id`,
    `service_data.entity_id` — as either a single string or a list of strings.
    Walk the whole structure rather than special-casing each shape.
    """
    found: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "entity_id":
                if isinstance(value, str):
                    found.add(value)
                elif isinstance(value, list):
                    found.update(v for v in value if isinstance(v, str))
            else:
                found.update(_extract_entity_ids(value))
    elif isinstance(obj, list):
        for item in obj:
            found.update(_extract_entity_ids(item))
    return found


@async_ttl_cache(300)
async def fetch_automation_index() -> dict[str, list[str]]:
    """Map entity_id -> [names of automations that reference it].

    This is the "critical section" context source: before NEXUS proposes or
    executes an action against an entity, it needs to know which automations
    already touch that entity (e.g. the christmas-tree-plug automation that
    turns tall_light_lr_christmas_tree_plug off nightly at 11:59pm) so a
    judge can avoid fighting an existing automation instead of just guessing.

    Enumerates automation.* entities from the cached fetch(), then does a
    best-effort GET per automation for its full config (triggers/conditions/
    actions, which is where target entity_ids live).

    Requests are SEQUENTIAL, not concurrent — mirrors the uptime job's lesson
    (backend/scheduler.py:_record_uptime) that firing many httpx calls at once
    thunders the event loop; one automation config at a time is cheap enough.

    Never raises. Degrades to a partial (or empty) dict on 401/timeout/any
    error, logging exactly ONE summary warning per call — not one per
    failure.
    """
    index: dict[str, list[str]] = {}
    error_count = 0
    try:
        data = await fetch()
        automations = [
            e for e in data.entities
            if e.get("entity_id", "").startswith("automation.")
        ]

        from backend.config import get_settings
        settings = get_settings()
        host = settings.hass_host
        token = settings.hass_token
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=10) as client:
            for ent in automations:
                unique_id = (ent.get("attributes") or {}).get("id")
                if not unique_id:
                    continue
                try:
                    resp = await client.get(
                        f"{host}/api/config/automation/config/{unique_id}",
                        headers=headers,
                    )
                    resp.raise_for_status()
                    config = resp.json()
                except Exception:
                    error_count += 1
                    continue

                name = (
                    config.get("alias")
                    or (ent.get("attributes") or {}).get("friendly_name")
                    or ent.get("entity_id")
                )
                for target_id in _extract_entity_ids(config):
                    index.setdefault(target_id, []).append(name)
    except Exception as e:
        logger.warning(f"fetch_automation_index degraded (best-effort): {e}")
        return index

    if error_count:
        logger.warning(
            f"fetch_automation_index: {error_count} automation config fetch(es) "
            "failed, returning partial index"
        )
    return index
