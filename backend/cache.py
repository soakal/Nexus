"""Shared async TTL cache with single-flight request coalescing.

Why this exists
---------------
The dashboard is a single-threaded asyncio app. Every open browser tab polls the
same integration endpoints every 10-15s, and each of those endpoints makes an
outbound LAN call (Home Assistant, Unraid, AdGuard, Channels DVR, weather...) with
a multi-second timeout. With N tabs open this becomes N independent outbound calls
per poll cycle — e.g. /api/sources/status fans out to 10 health checks, so 50 tabs
= 500 outbound calls every 15s. That saturates the event loop and the httpx
connection pool, and even trivial requests like /api/health get stuck behind the
backlog (the exact "everything is slow / offline" symptom).

This decorator fixes it by making all concurrent callers within one TTL window
share ONE outbound call and its cached result:

* TTL caching        — a fresh result is returned instantly without any I/O.
* Single-flight      — when the cache is cold, only the first caller does the
                       real fetch; everyone else awaits the same refresh.
* Negative caching   — failures (exceptions) and falsy results (e.g.
                       health_check() -> False) are cached too, so a down device
                       can't cause N serialized timeouts — but only for a SHORT
                       `falsy_ttl`, so a device that merely timed out on cold
                       first-contact flips back to green within a few seconds
                       instead of being stuck red for the full success TTL.
* .invalidate()      — force the next call to refresh, used after a mutating
                       action (HA service call, docker restart, filter toggle)
                       so the UI re-sync still sees ground truth immediately.

Assumes the wrapped coroutine's result does not depend on its arguments, which is
true for our arg-less integration fetch()/health_check() functions.
"""

import asyncio
import time
from functools import wraps

# Every decorated function's cache state, so all caches can be reset at once.
# Production code uses per-function .invalidate(); tests use reset_all_caches()
# to guarantee isolation (a cached health_check result must not leak across tests).
_CACHE_REGISTRY: list[dict] = []


def reset_all_caches() -> None:
    """Expire and clear every cache so the next call does a fresh fetch."""
    for state in _CACHE_REGISTRY:
        state.update(expires=0.0, ok=False, value=None, exc=None)


def async_ttl_cache(ttl: float, falsy_ttl: float | None = None):
    # A successful, truthy result is held for `ttl`. A failure (exception) or a
    # falsy result (e.g. a health check returning False) is held only for the
    # shorter `falsy_ttl` so a transiently-unreachable device recovers quickly.
    if falsy_ttl is None:
        falsy_ttl = min(ttl, 3.0)

    def decorator(fn):
        # expires: monotonic deadline; ok: was the last refresh a success;
        # value/exc: the cached success value or the cached exception to re-raise.
        state = {"expires": 0.0, "ok": False, "value": None, "exc": None}
        _CACHE_REGISTRY.append(state)
        lock = asyncio.Lock()

        def _return_cached():
            if state["ok"]:
                return state["value"]
            raise state["exc"]

        @wraps(fn)
        async def wrapper(*args, **kwargs):
            if time.monotonic() < state["expires"]:
                return _return_cached()
            async with lock:
                # Re-check inside the lock: a caller we queued behind may have
                # just refreshed the cache, in which case we reuse their result.
                if time.monotonic() < state["expires"]:
                    return _return_cached()
                try:
                    value = await fn(*args, **kwargs)
                    hold = ttl if value else falsy_ttl
                    state.update(expires=time.monotonic() + hold, ok=True, value=value, exc=None)
                    return value
                except Exception as e:
                    state.update(expires=time.monotonic() + falsy_ttl, ok=False, value=None, exc=e)
                    raise

        def invalidate():
            state["expires"] = 0.0

        wrapper.invalidate = invalidate
        return wrapper

    return decorator
