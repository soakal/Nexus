"""Single cached read model for the command-center dashboard."""

import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends

from backend.api.sources import REGISTRY_NAMES
from backend.auth import require_api_key
from backend.state_store import get_snapshots

router = APIRouter()

DASHBOARD_KEYS = (
    "dashboard.weather", "dashboard.adguard", "dashboard.channels",
    "dashboard.unraid", "dashboard.proxmox", "dashboard.proxmox_maintenance",
    "dashboard.brain", "dashboard.mail", "dashboard.briefing", "dashboard.today",
    "dashboard.claude_usage", "dashboard.openrouter",
)


@router.get("/state")
async def dashboard_state(_=Depends(require_api_key)):
    keys = [*(f"source.{name}" for name in REGISTRY_NAMES), *DASHBOARD_KEYS]
    snapshots = await asyncio.to_thread(get_snapshots, keys)
    sources = {}
    for name in REGISTRY_NAMES:
        snap = snapshots[f"source.{name}"]
        data = snap.get("data") or {}
        sources[name] = {
            "healthy": bool(data.get("healthy", False)),
            "freshness": snap["freshness"],
            "last_checked": snap["observed_at"],
            "error": snap["error"],
        }

    def item(name: str):
        snap = snapshots[f"dashboard.{name}"]
        return {
            "data": snap["data"],
            "freshness": snap["freshness"],
            "observed_at": snap["observed_at"],
            "error": snap["error"],
        }

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "sources": sources,
        "weather": item("weather"),
        "adguard": item("adguard"),
        "channels": item("channels"),
        "unraid": item("unraid"),
        "proxmox": item("proxmox"),
        "proxmox_maintenance": item("proxmox_maintenance"),
        "brain": item("brain"),
        "mail": item("mail"),
        "briefing": item("briefing"),
        "today": item("today"),
        "claude_usage": item("claude_usage"),
        "openrouter": item("openrouter"),
    }
