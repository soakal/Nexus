import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends

from backend.auth import require_api_key

router = APIRouter()

# Module-level (not function-local) so backend/safety/contracts.py's drift
# test can import the real registry name set instead of hardcoding a copy —
# a copy would silently stop matching reality the moment a 13th integration
# is added here without a matching contracts.py entry.
REGISTRY_NAMES = (
    "homeassistant", "unifi", "unraid", "obsidian", "github", "openrouter",
    "weather", "channels_dvr", "adguard", "hermes", "proxmox", "protonmail",
)


@router.get("/status")
async def sources_status(_=Depends(require_api_key)):
    from backend.integrations import (
        adguard,
        channels_dvr,
        github,
        hermes,
        homeassistant,
        obsidian,
        openrouter,
        protonmail,
        proxmox,
        unifi,
        unraid,
        weather,
    )

    sources = {
        "homeassistant": homeassistant,
        "unifi": unifi,
        "unraid": unraid,
        "obsidian": obsidian,
        "github": github,
        "openrouter": openrouter,
        "weather": weather,
        "channels_dvr": channels_dvr,
        "adguard": adguard,
        "hermes": hermes,
        "proxmox": proxmox,
        "protonmail": protonmail,
    }
    assert set(sources) == set(REGISTRY_NAMES), (
        "sources dict and REGISTRY_NAMES have drifted apart — keep them in sync"
    )

    results = await asyncio.gather(
        *[s.health_check() for s in sources.values()],
        return_exceptions=True,
    )

    status = {}
    for name, result in zip(sources.keys(), results, strict=False):
        healthy = isinstance(result, bool) and result
        status[name] = {"healthy": healthy, "last_checked": datetime.utcnow().isoformat()}

    return status
