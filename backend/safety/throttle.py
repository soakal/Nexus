"""Per-verb rate throttle + circuit breaker for broker writes (Tier 3 guardrails).

Process-local in-memory state — resets on restart, which is intentional for a
short-window guardrail (the window is ≤15 min; a restart clears stale state).

All functions are sync and thread-safe via a module-level lock. Use time.monotonic()
so the clock never goes backward (avoids issues with NTP or sleep/hibernate).

USER actions are NEVER subject to these guardrails — enforcement is the broker's
responsibility to call allow/record_* only for agent/autonomous actors.
"""

import time
import threading

_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}  # kind -> {"attempts": [float], "failures": [float], "trip_until": float}


def _st(kind: str) -> dict:
    """Return (creating if absent) the state bucket for `kind`. Must be called under _LOCK."""
    return _STATE.setdefault(kind, {"attempts": [], "failures": [], "trip_until": 0.0})


def allow(
    kind: str,
    *,
    max_per_window: int,
    window_s: int,
    now: float | None = None,
) -> tuple[bool, str | None]:
    """Check whether a dispatch of `kind` is currently allowed.

    Returns (True, None) when the dispatch may proceed.
    Returns (False, reason) when it is blocked, where reason is one of:
      'circuit_open' — breaker tripped; the kind is in its cooldown period.
      'throttled'    — rate cap reached within the rolling window.

    Does NOT record the attempt (call record_attempt() separately after allow()
    returns True and BEFORE the dispatcher fires, so the counter advances even
    when the dispatch is still in-flight).
    """
    now = now if now is not None else time.monotonic()
    with _LOCK:
        st = _st(kind)
        # Breaker check first: a tripped kind is flatly forbidden until cooldown expires.
        if st["trip_until"] > now:
            return False, "circuit_open"
        # Rate check: prune expired attempts, then check cap.
        st["attempts"] = [t for t in st["attempts"] if now - t < window_s]
        if len(st["attempts"]) >= max_per_window:
            return False, "throttled"
        return True, None


def record_attempt(kind: str, *, now: float | None = None) -> None:
    """Record that a dispatch of `kind` is about to fire (or just fired).

    Call this AFTER allow() returns True, BEFORE the dispatcher is awaited,
    so the counter advances regardless of the dispatch outcome.
    """
    now = now if now is not None else time.monotonic()
    with _LOCK:
        _st(kind)["attempts"].append(now)


def record_result(
    kind: str,
    success: bool,
    *,
    failure_threshold: int,
    window_s: int,
    cooldown_s: int,
    now: float | None = None,
) -> bool:
    """Record a dispatch outcome for `kind`.

    On success the failure streak is cleared (a working call resets the breaker
    counter — it does NOT re-open a tripped breaker, only prevents future trips).

    On failure, if `failure_threshold` failures have occurred within `window_s`,
    the breaker is TRIPPED: trip_until is set to now + cooldown_s and the failure
    list is cleared.

    Returns True the first time the breaker trips on this call; False otherwise.
    """
    now = now if now is not None else time.monotonic()
    with _LOCK:
        st = _st(kind)
        if success:
            # A successful dispatch resets the failure streak only.
            st["failures"] = []
            return False
        # Failure: prune stale failures, append this one.
        st["failures"] = [t for t in st["failures"] if now - t < window_s]
        st["failures"].append(now)
        if len(st["failures"]) >= failure_threshold:
            st["trip_until"] = now + cooldown_s
            st["failures"] = []
            return True
        return False


def reset() -> None:
    """Test hook — clear ALL throttle/breaker state.

    Call at the start of any test that exercises the throttle so state from
    prior tests (or default process-level counters) cannot interfere.
    """
    with _LOCK:
        _STATE.clear()
