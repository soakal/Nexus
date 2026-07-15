import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)


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
    async with httpx.AsyncClient(timeout=10) as client:
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

    return HAData(entities=entities, alerts=alerts, cloud_alerts=cloud_alerts)


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
        async with httpx.AsyncClient(timeout=5) as client:
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
    async with httpx.AsyncClient(timeout=15) as client:
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

        async with httpx.AsyncClient(timeout=10) as client:
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
