import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlmodel import Session, select

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)


@dataclass
class UniFiData:
    client_count: int = 0
    uplink_status: str = "unknown"
    bandwidth_mbps: float = 0.0
    alerts: list = field(default_factory=list)
    new_devices: list = field(default_factory=list)


async def fetch() -> UniFiData:
    from backend.config import get_settings
    from backend.database import KnownDevice, engine
    settings = get_settings()
    try:
        password = settings.unifi_password
    except Exception:
        raise Exception("UNIFI_PASSWORD not configured")

    headers = {"Accept": "application/json", "Content-Type": "application/json"}

    # UniFi uses cookie auth or API key depending on version
    async with httpx.AsyncClient(timeout=5, verify=False) as client:  # nosec B501 — UniFi uses self-signed LAN cert
        # Login
        login_resp = await client.post(
            f"{settings.unifi_host}/api/auth/login",
            json={"username": settings.unifi_username, "password": password},
            headers=headers,
        )
        if login_resp.status_code not in (200, 201):
            raise Exception(f"UniFi login failed: {login_resp.status_code}")

        # Get clients
        sites_resp = await client.get(f"{settings.unifi_host}/proxy/network/api/s/default/stat/sta", headers=headers)
        clients = []
        if sites_resp.status_code == 200:
            data = sites_resp.json()
            clients = data.get("data", [])

        # Uplink
        uplink_resp = await client.get(f"{settings.unifi_host}/proxy/network/api/s/default/stat/health", headers=headers)
        uplink_status = "ok"
        if uplink_resp.status_code == 200:
            health = uplink_resp.json().get("data", [])
            wan = next((h for h in health if h.get("subsystem") == "wan"), None)
            if wan:
                uplink_status = "ok" if wan.get("status") == "ok" else "degraded"

    # Check for new devices
    new_devices = []
    with Session(engine) as session:
        known = session.exec(select(KnownDevice)).all()
        known_macs = {d.mac for d in known}

        for client_dev in clients:
            mac = client_dev.get("mac", "")
            if mac and mac not in known_macs:
                new_devices.append({"mac": mac, "hostname": client_dev.get("hostname", "")})
                session.add(KnownDevice(mac=mac, hostname=client_dev.get("hostname", "")))
            elif mac:
                dev = session.exec(select(KnownDevice).where(KnownDevice.mac == mac)).first()
                if dev:
                    dev.last_seen = datetime.utcnow()
        session.commit()

    return UniFiData(
        client_count=len(clients),
        uplink_status=uplink_status,
        bandwidth_mbps=0.0,
        new_devices=new_devices,
    )


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        async with httpx.AsyncClient(timeout=2, verify=False) as client:  # nosec B501
            resp = await client.get(f"{settings.unifi_host}/", follow_redirects=True)
            return resp.status_code < 500
    except Exception:
        return False
