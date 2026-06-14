import logging
import time

import httpx

logger = logging.getLogger(__name__)


async def run_speedtest() -> dict:
    download_mbps = 0.0
    upload_mbps = 0.0
    ping_ms = 0.0

    # Ping: measure latency of a small request
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            t0 = time.monotonic()
            await client.get("https://speed.cloudflare.com/__down?bytes=0")
            elapsed = time.monotonic() - t0
            ping_ms = round(elapsed * 1000, 1)
    except Exception as e:
        logger.warning(f"Speedtest ping failed: {e}")

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
    }
