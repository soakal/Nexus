import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from backend.cache import async_ttl_cache

logger = logging.getLogger(__name__)


def _epoch_to_iso(ts) -> str:
    """Channels DVR returns epoch seconds; the frontend expects ISO strings."""
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@dataclass
class ChannelsData:
    recording_now: list = field(default_factory=list)
    upcoming: list = field(default_factory=list)
    library_shows: int = 0
    library_movies: int = 0
    storage_used_gb: float = 0.0
    storage_total_gb: float = 0.0
    failed_recordings: list = field(default_factory=list)


@async_ttl_cache(30)
async def fetch() -> ChannelsData:
    from backend.config import get_settings
    settings = get_settings()
    host = settings.channels_host

    async with httpx.AsyncClient(timeout=5) as client:
        data = ChannelsData()

        # Disk stats. GET /dvr returns a nested lowercase "disk" object with
        # byte counts, e.g. {"disk": {"total": ..., "free": ..., "used": ...}}.
        try:
            resp = await client.get(f"{host}/dvr")
            if resp.status_code != 200:
                raise RuntimeError(f"/dvr returned {resp.status_code}")
            dvr = resp.json() or {}
            disk = dvr.get("disk") or {}
            disk_total = disk.get("total", 0) or 0
            disk_free = disk.get("free", 0) or 0
            disk_used = disk.get("used") or max(disk_total - disk_free, 0)
            data.storage_total_gb = round(disk_total / 1024**3, 1)
            data.storage_used_gb = round(disk_used / 1024**3, 1)
        except Exception as e:
            # A failed disk-stats read must NOT be reported as real 0.0 GB storage —
            # that looks like data loss to the briefing/trends/proposer (same bug
            # that affected Unraid). Raise so callers treat Channels DVR as
            # UNAVAILABLE; the cache caches+re-raises briefly. (The /dvr endpoint is
            # also what health_check probes, so it is the right availability signal.)
            logger.warning(f"Channels DVR stats failed (reporting unavailable): {e}")
            raise RuntimeError(f"Channels DVR unavailable: {e}") from e

        # Recording jobs. The documented endpoint is /api/v1/jobs and fields are
        # snake_case: id, name, start_time/end_time (epoch seconds), duration,
        # channel, channels[], skipped, failed, and a nested "item" with "title".
        # A job is recording right now when start_time <= now < end_time.
        try:
            resp = await client.get(f"{host}/api/v1/jobs")
            if resp.status_code != 200:
                raise RuntimeError(f"/api/v1/jobs returned {resp.status_code}")
            jobs = resp.json() or []
            now = time.time()
            day_ago = now - 86400
            active, upcoming, failed = [], [], []
            for j in jobs:
                item = j.get("item") or {}
                channels_list = j.get("channels") or []
                start_ts = j.get("start_time") or 0
                end_ts = j.get("end_time") or 0
                entry = {
                    "title": item.get("title") or j.get("name", ""),
                    "channel": j.get("channel") or (channels_list[0] if channels_list else ""),
                    "start": _epoch_to_iso(start_ts),
                    "end": _epoch_to_iso(end_ts),
                    "program_id": str(j.get("id", "")),
                }
                if j.get("skipped") or j.get("failed"):
                    # Surface only RECENT failures (last 24h) so the list stays
                    # relevant and bounded — old skips aren't actionable.
                    if max(start_ts, end_ts) >= day_ago:
                        reason = "skipped" if j.get("skipped") else "failed"
                        failed.append((start_ts, {**entry, "reason": reason}))
                    continue
                if start_ts <= now < end_ts:
                    active.append((start_ts, entry))
                elif start_ts > now:
                    upcoming.append((start_ts, entry))
            data.recording_now = [e for _, e in sorted(active, key=lambda t: t[0])]
            data.upcoming = [e for _, e in sorted(upcoming, key=lambda t: t[0])[:10]]
            data.failed_recordings = [e for _, e in sorted(failed, key=lambda t: t[0], reverse=True)[:10]]
        except Exception as e:
            # Consistent with the disk-stats block above: a failed jobs read must
            # NOT silently look like "nothing recording, nothing failed" — raise so
            # the whole integration reports UNAVAILABLE instead.
            logger.warning(f"Channels DVR jobs unavailable (reporting unavailable): {e}")
            raise RuntimeError(f"Channels DVR jobs unavailable: {e}") from e

    return data


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        from backend.config import get_settings
        settings = get_settings()
        async with httpx.AsyncClient(timeout=5) as client:
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
