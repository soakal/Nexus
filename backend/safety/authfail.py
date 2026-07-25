"""Bounded in-process counter of failed API-key authentications (401s).

Recorded from backend/auth.py's existing 401 branch; read once per 5-minute
watchdog tick by backend/agents/watchdog.py::check_auth_failure_burst.

Process-local and deliberately volatile: this is a short-window (~30 min)
burst detector, so a restart clearing it is correct. The EDGE state (have we
already paged about this source) is the part that must survive restarts, and
that lives in SystemState — see governor.claim_auth_burst_alert.

HARD BOUNDS ARE A SECURITY REQUIREMENT, NOT AN OPTIMISATION: the 401 path is
reachable pre-auth by anyone who can reach 0.0.0.0:8000, so both the number of
tracked sources and the per-source history are capped. An unbounded dict here
would let the storm we are trying to detect exhaust memory instead.
"""

import threading
import time
from collections import Counter, deque

_LOCK = threading.Lock()
MAX_SOURCES = 64  # distinct client identities tracked; oldest-touched evicted
MAX_EVENTS_PER_SOURCE = 512  # ring buffer per source

_STATE: dict[str, deque] = {}  # source -> deque[(monotonic_ts, path)], maxlen=MAX_EVENTS_PER_SOURCE


def record_failure(source: str, path: str, *, now: float | None = None) -> None:
    """Record one failed auth attempt. Never raises."""
    try:
        now = now if now is not None else time.monotonic()
        source = (source or "unknown")[:64]
        path = (path or "?")[:64]
        with _LOCK:
            if source not in _STATE and len(_STATE) >= MAX_SOURCES:
                # Evict the source whose most recent event is oldest.
                oldest_source = min(
                    _STATE,
                    key=lambda s: _STATE[s][-1][0] if _STATE[s] else 0.0,
                )
                del _STATE[oldest_source]
            bucket = _STATE.setdefault(source, deque(maxlen=MAX_EVENTS_PER_SOURCE))
            bucket.append((now, path))
    except Exception:
        pass


def recent(window_s: float, *, now: float | None = None) -> dict[str, dict]:
    """Return {source: {"count": int, "paths": [(path, n), ...]}} for sources
    with at least one failure in the last `window_s` seconds. `paths` is the
    top 3 most common paths in-window, descending by count."""
    now = now if now is not None else time.monotonic()
    result: dict[str, dict] = {}
    with _LOCK:
        for source, events in _STATE.items():
            in_window = [p for (t, p) in events if now - t < window_s]
            if not in_window:
                continue
            result[source] = {
                "count": len(in_window),
                "paths": Counter(in_window).most_common(3),
            }
    return result


def reset() -> None:
    """Test hook — clear all tracked failures."""
    with _LOCK:
        _STATE.clear()
