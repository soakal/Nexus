"""Text-to-image via Pollinations.ai (Hermes Phase 8 port).

Ported from hermes-agent/image_gen.py. Unauthenticated public API — no vault
secret, no NEXUS_API_KEY equivalent. Modeled on sports.py: no
@async_ttl_cache (every prompt is unique, caching would be actively wrong),
one outer try/except, never raises.
"""
import logging
from urllib.parse import quote

import httpx

from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)

POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}?width=1024&height=1024&nologo=true"


async def generate_image(prompt: str) -> bytes | None:
    """Raw image bytes for `prompt`, or None if generation failed for any
    reason (non-200, non-image response, transport error). Never raises."""
    url = POLLINATIONS_URL.format(prompt=quote(prompt, safe=""))
    try:
        async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=60) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            logger.debug(f"generate_image failed: HTTP {resp.status_code}")
            return None
        if "image" not in resp.headers.get("content-type", ""):
            logger.debug(f"generate_image failed: unexpected content-type {resp.headers.get('content-type')!r}")
            return None
        return resp.content
    except Exception as e:
        logger.debug(f"generate_image failed (ignored): {e}")
        return None
