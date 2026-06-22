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
    "light.tall_light_lr_christmas_tree_plug",
    "switch.tall_light_lr_christmas_tree_plug",
    "light.left_porch_light",
    "light.right_porch_light",
    "light.left_garage_light",
    "light.right_garage_light",
    # Garage door
    "cover.garage_door_garage_door",
    # August / back door lock
    "lock.dining_room",
    # UniFi integration health
    "switch.unifi_network",
    "sensor.unifi_identity",
    "switch.unifi_identity",
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
    async with httpx.AsyncClient(timeout=5) as client:
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
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{settings.hass_host}/api/", headers=headers)
            return resp.status_code == 200
    except Exception:
        return False


async def call_service(domain: str, service: str, data: dict | None = None) -> dict:
    from backend.config import get_settings
    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.hass_token}", "Content-Type": "application/json"}
    url = f"{settings.hass_host}/api/services/{domain}/{service}"
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(url, json=data or {}, headers=headers)
        resp.raise_for_status()
        result = resp.json()
    # The cached entity snapshot is now stale (we just changed a device); drop it
    # so the frontend's post-toggle reload sees the real new state, not the cache.
    fetch.invalidate()
    return result
