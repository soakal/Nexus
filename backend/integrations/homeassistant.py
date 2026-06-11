import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


@dataclass
class HAData:
    entities: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    cloud_alerts: list = field(default_factory=list)
    last_updated: datetime = field(default_factory=datetime.utcnow)


class IntegrationError(Exception):
    pass


# Domains that are inherently non-persistent: they never hold a stable
# "available" state, so an "unavailable"/"unknown" reading is expected and
# should NOT be flagged as a real problem.
_NON_PERSISTENT_DOMAINS = {
    "stt",          # speech-to-text: only active while speaking
    "tts",          # text-to-speech: only active while speaking
    "input_button", # stateless helper, fires events
    "scene",        # activation-only, no persistent state
    "script",       # idle scripts report no meaningful state
    "update",       # up-to-date entities report "off"/"unknown"
}

# Domains that genuinely indicate a degraded device when "unavailable".
_DEGRADABLE_DOMAINS = {
    "device_tracker",
    "person",
    "media_player",
    "binary_sensor",
    "sensor",
}


def is_noise_entity(entity_id: str, state) -> bool:
    """Return True when an unavailable/unknown reading is *expected* (noise),
    rather than a genuine fault worth alerting on.

    Only entities whose state is "unavailable"/"unknown" reach the alert
    pipeline, so this function decides which of those to suppress.
    """
    if not entity_id:
        return False

    domain = entity_id.split(".", 1)[0]
    state_l = (state or "").lower() if isinstance(state, str) else ""

    # Inherently non-persistent domains never have a real "available" state.
    if domain in _NON_PERSISTENT_DOMAINS:
        return True

    # Any "battery" helper/entity is treated as noise (input_button.battery is
    # a helper, not a real sensor). Match on entity_id (friendly_name lives in
    # attributes and is checked by the caller-aware variant below).
    if "battery" in entity_id.lower():
        return True

    # A sensor reporting "unknown" usually just means "not yet read" (e.g.
    # first boot) — that is not a degradation. "unavailable" still alerts.
    if state_l == "unknown" and domain in _DEGRADABLE_DOMAINS:
        return True

    return False


def _is_noise(entity: dict) -> bool:
    """Entity-aware noise check that also inspects friendly_name."""
    entity_id = entity.get("entity_id", "")
    state = entity.get("state")
    friendly = ""
    attrs = entity.get("attributes")
    if isinstance(attrs, dict):
        friendly = str(attrs.get("friendly_name") or "")

    if "battery" in friendly.lower():
        return True

    return is_noise_entity(entity_id, state)


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

        # Smart alert filter: only flag genuinely degraded entities.
        alerts = [
            e["entity_id"]
            for e in entities
            if e.get("state") in ("unavailable", "unknown")
            and not _is_noise(e)
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
        return resp.json()
