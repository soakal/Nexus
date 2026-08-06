import base64
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlmodel import Session, select

from backend.cache import async_ttl_cache
from backend.integrations.unifi_tls_pinning import build_transport

logger = logging.getLogger(__name__)

_MAC_HEX_RE = re.compile(r"^[0-9a-fA-F]{12}$")


@dataclass
class UniFiData:
    client_count: int = 0
    uplink_status: str = "unknown"
    bandwidth_mbps: float = 0.0
    # None means "the alarms read failed this cycle" -- deliberately distinct
    # from [] ("confirmed no active alarms"). See fetch()'s alarms try/except
    # for why this must never quietly collapse to a false all-clear.
    alerts: list | None = field(default_factory=list)
    new_devices: list = field(default_factory=list)


async def _login(client: httpx.AsyncClient) -> dict:
    """Log into the UniFi controller and return headers for subsequent requests.

    GETs (fetch/health_check) only ever needed session cookies, which httpx's
    client handles automatically -- but mutating POSTs (block/unblock) also
    require an X-CSRF-Token header, or UniFi OS rejects them. Captured from
    the login response header when present, else decoded out of the `TOKEN`
    cookie's JWT payload (UniFi OS puts a `csrfToken` claim there) -- both
    paths are exercised by real controllers depending on firmware version.
    """
    from backend.config import get_settings
    settings = get_settings()
    try:
        password = settings.unifi_password
    except Exception:
        raise Exception("UNIFI_PASSWORD not configured")

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    login_resp = await client.post(
        f"{settings.unifi_host}/api/auth/login",
        json={"username": settings.unifi_username, "password": password},
        headers=headers,
    )
    if login_resp.status_code not in (200, 201):
        raise Exception(f"UniFi login failed: {login_resp.status_code}")

    csrf = login_resp.headers.get("X-CSRF-Token")
    if not csrf:
        token_cookie = login_resp.cookies.get("TOKEN")
        if token_cookie:
            try:
                payload_b64 = token_cookie.split(".")[1]
                padding = "=" * (-len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
                csrf = payload.get("csrfToken")
            except Exception:
                csrf = None

    result_headers = dict(headers)
    if csrf:
        result_headers["X-CSRF-Token"] = csrf
    return result_headers


def _normalize_mac(value: str) -> str:
    """Accepts aa:bb:cc:dd:ee:ff, aa-bb-cc-dd-ee-ff, aabb.ccdd.eeff, or bare
    aabbccddeeff -- returns lowercase colon form. Raises ValueError on
    anything that isn't exactly 6 octets of hex, so a malformed/injected
    value can never reach the stamgr POST body."""
    if not value:
        raise ValueError("empty MAC address")
    stripped = re.sub(r"[:\-.]", "", value)
    if not _MAC_HEX_RE.match(stripped):
        raise ValueError(f"invalid MAC address: {value!r}")
    pairs = [stripped[i:i + 2] for i in range(0, 12, 2)]
    return ":".join(pairs).lower()


async def _stamgr_cmd(cmd: str, mac: str) -> dict:
    """POST a station-manager command (block-sta/unblock-sta) for one client.

    Raises on any failure (non-2xx, or a 200 whose body reports rc != "ok")
    -- the broker needs a real failure signal, not a swallowed False, or a
    failed block/unblock would still get recorded EXECUTED."""
    from backend.config import get_settings
    settings = get_settings()
    normalized = _normalize_mac(mac)

    async with httpx.AsyncClient(timeout=5, transport=build_transport()) as client:
        headers = await _login(client)
        resp = await client.post(
            f"{settings.unifi_host}/proxy/network/api/s/default/cmd/stamgr",
            json={"cmd": cmd, "mac": normalized},
            headers=headers,
        )
        if resp.status_code != 200:
            raise Exception(f"UniFi {cmd} failed: HTTP {resp.status_code}")
        body = resp.json()
        if (body.get("meta") or {}).get("rc") != "ok":
            raise Exception(f"UniFi {cmd} failed: {body.get('meta')}")

    fetch.invalidate()
    return body


async def block_client(mac: str) -> dict:
    return await _stamgr_cmd("block-sta", mac)


async def unblock_client(mac: str) -> dict:
    return await _stamgr_cmd("unblock-sta", mac)


@async_ttl_cache(60)
async def fetch() -> UniFiData:
    from backend.config import get_settings
    from backend.database import KnownDevice, engine
    settings = get_settings()

    # UniFi uses cookie auth or API key depending on version
    async with httpx.AsyncClient(timeout=5, transport=build_transport()) as client:
        headers = await _login(client)

        # Get clients — raise on failure rather than defaulting to 0 clients
        # (the "Unraid lesson": a zero-default here looks like a dead AP, not
        # an unreachable one).
        sites_resp = await client.get(f"{settings.unifi_host}/proxy/network/api/s/default/stat/sta", headers=headers)
        if sites_resp.status_code != 200:
            raise Exception(f"UniFi clients fetch failed: {sites_resp.status_code}")
        clients = sites_resp.json().get("data", [])

        # Uplink — same lesson: a failed health call must not silently read as "ok".
        uplink_resp = await client.get(f"{settings.unifi_host}/proxy/network/api/s/default/stat/health", headers=headers)
        if uplink_resp.status_code != 200:
            raise Exception(f"UniFi health fetch failed: {uplink_resp.status_code}")
        health = uplink_resp.json().get("data", [])
        wan = next((h for h in health if h.get("subsystem") == "wan"), None)
        uplink_status = ("ok" if wan.get("status") == "ok" else "degraded") if wan else "unknown"

        # Live-verified 2026-07-29: the same `stat/health` response's `wan`
        # subsystem entry already carries `tx_bytes-r`/`rx_bytes-r` — the
        # controller's current throughput RATE in bytes/sec (confirmed live
        # against the real Dream Machine Pro Max; values moved between polls
        # like a real gauge, not a static counter). No second endpoint needed.
        # The site-wide `stat/report/5minutes.site` report endpoint was also
        # tried live and only returns historical rollups (stale/zeroed for
        # the current window), not a current rate — unsuitable here.
        # A `wan` entry missing OR null tx/rx values (older controller, or a
        # momentarily-null gauge reading) degrades to 0.0 -- `.get(key) or 0`,
        # not `.get(key, 0)`, since an explicit `null` isn't caught by the
        # dict-default form and `None + None` raises TypeError, which would
        # wrongly read as a full UniFi outage via the outer exception path.
        if wan:
            bandwidth_mbps = round(
                ((wan.get("tx_bytes-r") or 0) + (wan.get("rx_bytes-r") or 0)) * 8 / 1_000_000, 2
            )
        else:
            bandwidth_mbps = 0.0

        # Active alerts — UniFi's `list/alarm` endpoint returns ALL alarms
        # (active + archived) by convention -- the caller filters, typically
        # via an `archived` query param or post-filtering `archived`/
        # `archived_time` on each row. No alarm existed on the real controller
        # during the ORIGINAL live verification to confirm the exact field
        # name, so this filters defensively on the common `archived` boolean
        # (a no-op, not a false-negative, if that key is absent) rather than
        # trusting an unverified "no archived param = active-only" assumption.
        # Raw dicts are otherwise kept as-is — tools.py's consumer renders
        # arbitrary alert values via str(a).
        #
        # FIXED 2026-08-06: `list/alarm` was live-verified BROKEN on this
        # controller (UniFi Network 10.5.67 / UDM Pro Max) -- returns
        # HTTP 400 {"meta":{"rc":"error","msg":"api.err.InvalidObject"}} for
        # EVERY request, meaning "alarm" is no longer (or never was, on this
        # firmware) a valid record type -- confirmed by testing `list/wlanconf`/
        # `list/networkconf`/`list/user` on the same controller, which all
        # return 200, proving the legacy `list/*` REST pattern itself still
        # works; only "alarm" specifically is rejected. No working replacement
        # record-type name was found (list/event, list/alert, stat/alarm,
        # stat/event, the v2 `/proxy/network/v2/api/site/default/alarm` path,
        # and a raw-Tomcat-404 variant were all tried live and failed too).
        #
        # This ONE sub-call is therefore isolated in its own try/except,
        # degrading to alerts=None (never [] -- see UniFiData.alerts'
        # docstring: an empty list must never be confused with "confirmed no
        # alarms" when the read itself is broken) rather than raising and
        # killing client_count/uplink_status/bandwidth_mbps too, which all
        # still work fine and are real, useful data on every poll. This is a
        # deliberate, narrow exception to the "Unraid lesson" raise-on-failure
        # convention used elsewhere in this function -- clients/health above
        # still raise, because those calls are confirmed genuinely working.
        try:
            alarm_resp = await client.get(f"{settings.unifi_host}/proxy/network/api/s/default/list/alarm", headers=headers)
            if alarm_resp.status_code != 200:
                raise Exception(f"UniFi alarms fetch failed: {alarm_resp.status_code}")
            alerts = [a for a in alarm_resp.json().get("data", []) if not a.get("archived")]
        except Exception as e:
            logger.warning(f"UniFi alarms read failed (degrading alerts to unavailable, not a false all-clear): {e}")
            alerts = None

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
        bandwidth_mbps=bandwidth_mbps,
        alerts=alerts,
        new_devices=new_devices,
    )


@async_ttl_cache(30)
async def health_check() -> bool:
    """Exercises the same login path fetch() depends on — an unauthenticated root
    ping would report "healthy" even with a wrong/expired UNIFI_PASSWORD, which
    fetch() would then fail on."""
    try:
        from backend.config import get_settings
        settings = get_settings()
        password = settings.unifi_password
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=5, transport=build_transport()) as client:
            resp = await client.post(
                f"{settings.unifi_host}/api/auth/login",
                json={"username": settings.unifi_username, "password": password},
                headers=headers,
            )
            return resp.status_code in (200, 201)
    except Exception:
        return False
