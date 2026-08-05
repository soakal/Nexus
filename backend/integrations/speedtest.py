import logging
import time

import httpx

logger = logging.getLogger(__name__)

# Paid once at import instead of on every httpx.AsyncClient() construction.
# Verified 2026-08-05: httpx builds an SSL context synchronously inside
# AsyncClient(), which costs ~1.3-1.45s on this host (Defender scanning the
# certifi bundle) and blocks the event loop while doing it -- this, not
# network latency, was why ping_ms recorded 2.5-7.9 SECONDS (other coroutines
# building their own httpx clients around the same time delayed this
# function's resume). Reused across all three phases below.
_SSL_CONTEXT = httpx.create_ssl_context()


async def run_speedtest() -> dict:
    download_mbps = 0.0
    upload_mbps = 0.0
    ping_ms = 0.0

    try:
        async with httpx.AsyncClient(verify=_SSL_CONTEXT, timeout=10) as client:
            # First request doubles as the connectivity probe (if even this
            # fails we're almost certainly offline, e.g. right after boot)
            # AND warms the TCP/TLS connection -- its timing includes
            # handshake cost, so it is deliberately NOT reported as ping_ms.
            await client.get("https://speed.cloudflare.com/__down?bytes=0")

            # Real ping: 3 more round-trips over the now-warm connection,
            # take the min (excludes handshake entirely, and the min shakes
            # out transient scheduling jitter rather than averaging it in).
            pings = []
            for _ in range(3):
                t0 = time.monotonic()
                await client.get("https://speed.cloudflare.com/__down?bytes=0")
                pings.append(time.monotonic() - t0)
            ping_ms = round(min(pings) * 1000, 1)

            # Download: GET 25MB, same warm connection (no repeated handshake
            # cost distorting the timed transfer window).
            try:
                t0 = time.monotonic()
                resp = await client.get(
                    "https://speed.cloudflare.com/__down?bytes=25000000", timeout=30
                )
                elapsed = time.monotonic() - t0
                bytes_received = len(resp.content)
                if elapsed > 0:
                    download_mbps = round((bytes_received * 8) / elapsed / 1_000_000, 1)
            except Exception as e:
                logger.warning(f"Speedtest download failed: {e}")

            # Upload: POST ~5MB, same warm connection. A fixed setup cost
            # would distort this smaller payload far more than the 25MB
            # download (which is exactly the lopsided pattern the unfixed
            # code produced), so reusing the connection matters more here.
            try:
                payload = b"\x00" * 5_000_000
                t0 = time.monotonic()
                await client.post("https://speed.cloudflare.com/__up", content=payload, timeout=30)
                elapsed = time.monotonic() - t0
                if elapsed > 0:
                    upload_mbps = round((len(payload) * 8) / elapsed / 1_000_000, 1)
            except Exception as e:
                logger.warning(f"Speedtest upload failed: {e}")
                upload_mbps = 0.0
    except Exception as e:
        logger.warning(f"Speedtest ping failed (likely offline), skipping run: {e}")
        return {"download_mbps": 0.0, "upload_mbps": 0.0, "ping_ms": 0.0, "online": False}

    return {
        "download_mbps": download_mbps,
        "upload_mbps": upload_mbps,
        "ping_ms": ping_ms,
        "online": True,
    }
