import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)


@dataclass
class HAData:
    entities: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    last_updated: datetime = field(default_factory=datetime.utcnow)


class IntegrationError(Exception):
    pass


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

    alerts = [e["entity_id"] for e in entities if e.get("state") in ("unavailable", "unknown")]
    return HAData(entities=entities, alerts=alerts)


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
