import asyncio
import logging
from dataclasses import dataclass

import httpx

from backend.cache import async_ttl_cache
from backend.http_client import SSL_CONTEXT

logger = logging.getLogger(__name__)


@dataclass
class OpenRouterData:
    available: bool = False
    model_count: int = 0
    # Credit/usage fields (2026-08-05) -- live-verified against the real
    # OPENROUTER_API_KEY's GET /api/v1/key response shape before writing this,
    # not guessed from docs alone. limit/limit_remaining are None for an
    # unlimited key (OpenRouter's own convention -- never coerce to 0, which
    # would misrepresent "no cap" as "no credit left").
    credit_limit: float | None = None
    credit_remaining: float | None = None
    usage: float = 0.0
    usage_daily: float = 0.0
    is_free_tier: bool = False


@async_ttl_cache(30)
async def fetch() -> OpenRouterData:
    # Added 2026-08-05 alongside the dashboard.openrouter collector: fetch()
    # is now called by THREE independent schedulers (the 600s dashboard
    # collector, the 5-min contract canary reading this cache, and whatever
    # else calls it going forward) instead of zero, and each call is now TWO
    # requests (models + key), one of which returns OpenRouter's full model
    # catalog. Uncached, that would have quietly turned "1 request/5min" into
    # "6 requests/5min" the moment openrouter left backend/safety/contracts.py's
    # EXCLUDED list -- matching every other integration's fetch()'s convention.
    return await _get_data()


async def _get_data() -> OpenRouterData:
    from backend.config import get_settings
    settings = get_settings()
    try:
        api_key = settings.openrouter_api_key
    except Exception:
        raise Exception("OPENROUTER_API_KEY not configured")

    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(verify=SSL_CONTEXT, timeout=5) as client:
        # return_exceptions=True: a bare gather() propagates the FIRST
        # exception but does not cancel or await the other request, so the
        # `async with` block above would close the client out from under a
        # still-in-flight sibling call -- "Task exception was never
        # retrieved" noise in the log on any one-sided failure. Waiting for
        # both, then re-raising, keeps the same "raise on any failure"
        # contract without that orphaned task.
        models_resp, key_resp = await asyncio.gather(
            client.get("https://openrouter.ai/api/v1/models", headers=headers),
            client.get("https://openrouter.ai/api/v1/key", headers=headers),
            return_exceptions=True,
        )
        for resp in (models_resp, key_resp):
            if isinstance(resp, BaseException):
                raise resp
        models_resp.raise_for_status()
        key_resp.raise_for_status()
        models = models_resp.json().get("data", [])
        key_data = key_resp.json().get("data") or {}

    return OpenRouterData(
        available=True,
        model_count=len(models),
        credit_limit=key_data.get("limit"),
        credit_remaining=key_data.get("limit_remaining"),
        usage=key_data.get("usage") or 0.0,
        usage_daily=key_data.get("usage_daily") or 0.0,
        is_free_tier=bool(key_data.get("is_free_tier", False)),
    )


@async_ttl_cache(30)
async def health_check() -> bool:
    try:
        data = await _get_data()
        return data.available
    except Exception:
        return False
