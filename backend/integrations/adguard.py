import asyncio
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class AdGuardData:
    queries_today: int = 0
    blocked_today: int = 0
    blocked_pct: float = 0.0
    top_blocked: list = field(default_factory=list)
    top_clients: list = field(default_factory=list)
    filtering_enabled: bool = True


def _auth(settings):
    return (settings.adguard_user, _get_adguard_pass(settings))


def _get_adguard_pass(settings):
    try:
        from backend.secrets.manager import get_secret
        return get_secret("ADGUARD_PASS")
    except Exception:
        return ""


async def fetch() -> AdGuardData:
    from backend.config import get_settings
    settings = get_settings()
    host = settings.adguard_host

    async with httpx.AsyncClient(timeout=5) as client:
        auth = _auth(settings)
        resp = await client.get(f"{host}/control/stats", auth=auth)
        resp.raise_for_status()
        stats = resp.json()

        resp2 = await client.get(f"{host}/control/status", auth=auth)
        filtering_enabled = True
        if resp2.status_code == 200:
            filtering_enabled = resp2.json().get("protection_enabled", True)

    total = stats.get("num_dns_queries", 0)
    blocked = stats.get("num_blocked_filtering", 0)
    blocked_pct = round(blocked / total * 100, 1) if total > 0 else 0.0

    return AdGuardData(
        queries_today=total,
        blocked_today=blocked,
        blocked_pct=blocked_pct,
        top_blocked=[{"domain": k, "count": v} for item in (stats.get("top_blocked_domains") or []) for k, v in (item.items() if isinstance(item, dict) else {}.items())][:10],
        top_clients=[{"name": k, "count": v} for item in (stats.get("top_clients") or []) for k, v in (item.items() if isinstance(item, dict) else {}.items())][:10],
        filtering_enabled=filtering_enabled,
    )


async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{settings.adguard_host}/control/stats", auth=_auth(settings))
            return resp.status_code == 200
    except Exception:
        return False


async def set_filtering(enabled: bool) -> None:
    from backend.config import get_settings
    settings = get_settings()
    async with httpx.AsyncClient(timeout=5) as client:
        await client.post(
            f"{settings.adguard_host}/control/dns_config",
            json={"protection_enabled": enabled},
            auth=_auth(settings),
        )


_reenable_task = None


async def disable_for_minutes(minutes: int) -> None:
    global _reenable_task
    await set_filtering(False)

    if _reenable_task and not _reenable_task.done():
        _reenable_task.cancel()

    async def reenable():
        await asyncio.sleep(minutes * 60)
        try:
            await set_filtering(True)
            logger.info(f"AdGuard filtering re-enabled after {minutes} min")
        except Exception as e:
            logger.error(f"Failed to re-enable AdGuard: {e}")

    _reenable_task = asyncio.create_task(reenable())
