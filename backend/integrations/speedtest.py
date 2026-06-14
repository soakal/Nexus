import logging
import time

import httpx

logger = logging.getLogger(__name__)


async def run_speedtest() -> dict:
    download_mbps = 0.0
    upload_mbps = 0.0
    ping_ms = 0.0

    # Ping doubles as a connectivity probe. If even this tiny request fails we are
    # almost certainly offline (e.g. right after a boot, before the network is up),
    # so skip the heavy transfers and signal `online: False` — the caller then skips
    # recording, instead of polluting the trend history with a 0-mbps sample.
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            t0 = time.monotonic()
            await client.get("https://speed.cloudflare.com/__down?bytes=0")
            elapsed = time.monotonic() - t0
            ping_ms = round(elapsed * 1000, 1)
    except Exception as e:
        logger.warning(f"Speedtest ping failed (likely offline), skipping run: {e}")
        return {"download_mbps": 0.0, "upload_mbps": 0.0, "ping_ms": 0.0, "online": False}

    # Download: GET 25MB from Cloudflare speed test endpoint
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            t0 = time.monotonic()
            resp = await client.get("https://speed.cloudflare.com/__down?bytes=25000000")
            elapsed = time.monotonic() - t0
            bytes_received = len(resp.content)
            if elapsed > 0:
                download_mbps = round((bytes_received * 8) / elapsed / 1_000_000, 1)
    except Exception as e:
        logger.warning(f"Speedtest download failed: {e}")

    # Upload: POST ~5MB to Cloudflare speed test endpoint
    try:
        payload = b"\x00" * 5_000_000
        async with httpx.AsyncClient(timeout=30) as client:
            t0 = time.monotonic()
            resp = await client.post("https://speed.cloudflare.com/__up", content=payload)
            elapsed = time.monotonic() - t0
            if elapsed > 0:
                upload_mbps = round((len(payload) * 8) / elapsed / 1_000_000, 1)
    except Exception as e:
        logger.warning(f"Speedtest upload failed: {e}")
        upload_mbps = 0.0

    return {
        "download_mbps": download_mbps,
        "upload_mbps": upload_mbps,
        "ping_ms": ping_ms,
        "online": True,
    }
