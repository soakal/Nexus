import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)


@dataclass
class ChannelsData:
    recording_now: list = field(default_factory=list)
    upcoming: list = field(default_factory=list)
    library_shows: int = 0
    library_movies: int = 0
    storage_used_gb: float = 0.0
    storage_total_gb: float = 0.0


async def fetch() -> ChannelsData:
    from backend.config import get_settings
    settings = get_settings()
    host = settings.channels_host

    async with httpx.AsyncClient(timeout=5) as client:
        data = ChannelsData()

        # Library stats
        try:
            resp = await client.get(f"{host}/dvr")
            if resp.status_code == 200:
                dvr = resp.json()
                data.storage_used_gb = round(dvr.get("storage_used", 0) / 1024**3, 1)
                data.storage_total_gb = round(dvr.get("storage_total", 0) / 1024**3, 1)
        except Exception as e:
            logger.warning(f"Channels DVR stats failed: {e}")

        # Active recordings
        try:
            resp = await client.get(f"{host}/dvr/jobs")
            if resp.status_code == 200:
                jobs = resp.json()
                data.recording_now = [
                    {"title": j.get("airing", {}).get("title", ""), "channel": j.get("airing", {}).get("channel", ""), "start": j.get("start", "")}
                    for j in jobs if j.get("status") == "active"
                ]
        except Exception as e:
            logger.warning(f"Channels active recordings failed: {e}")

        # Upcoming recordings
        try:
            resp = await client.get(f"{host}/devices/ANY/guide/jobs")
            if resp.status_code == 200:
                jobs = resp.json()
                data.upcoming = [
                    {"title": j.get("airing", {}).get("title", ""), "channel": j.get("airing", {}).get("channel", ""), "start": j.get("start", ""), "end": j.get("end", "")}
                    for j in jobs[:10]
                ]
        except Exception as e:
            logger.warning(f"Channels upcoming failed: {e}")

    return data


async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(f"{settings.channels_host}/dvr")
            return resp.status_code == 200
    except Exception:
        return False


async def trigger_recording(program_id: str) -> dict:
    from backend.config import get_settings
    settings = get_settings()
    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(f"{settings.channels_host}/dvr/guide/jobs", json={"program_id": program_id})
        if resp.status_code == 404:
            raise ValueError(f"Program {program_id} not found")
        resp.raise_for_status()
        return resp.json()
