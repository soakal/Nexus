import hashlib
import hmac
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from backend.auth import require_api_key

router = APIRouter()


class TriggerRequest(BaseModel):
    task_name: str
    parameters: dict = {}


# Process-local fixed-window rate limiter for /api/trigger. Hermes is the only
# caller; this caps abuse if the Bearer key leaks. Window is 60s, max 5 calls.
# A reset hook keeps the autouse test fixtures from tripping across tests.
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW_S = 60.0
_rate_state = {"window_start": 0.0, "count": 0}


def _reset_rate_limit() -> None:
    """Test hook — clear the rate-limit window so tests don't trip each other."""
    _rate_state["window_start"] = 0.0
    _rate_state["count"] = 0


def _check_rate_limit() -> None:
    now = time.monotonic()
    if now - _rate_state["window_start"] >= _RATE_LIMIT_WINDOW_S:
        _rate_state["window_start"] = now
        _rate_state["count"] = 0
    _rate_state["count"] += 1
    if _rate_state["count"] > _RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Too many trigger requests")


# ---------------------------------------------------------------------------
# HMAC-SHA256 signature helpers (Tier 1.6 autonomy ingress hardening)
# ---------------------------------------------------------------------------
# Header names: X-Timestamp (unix epoch seconds as a string)
#               X-Signature (lowercase hex digest)
# Signing envelope: HMAC-SHA256(key=secret, msg=timestamp_bytes + b"." + raw_body)
# ---------------------------------------------------------------------------

def compute_trigger_signature(secret: str, timestamp: str, body: bytes) -> str:
    """Return the HMAC-SHA256 hex digest over '{timestamp}.' + raw body.

    Both the sender (Hermes) and the receiver (this endpoint) call this function
    with the same inputs to produce and compare digests.
    """
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return mac.hexdigest()


def _verify_trigger_hmac(timestamp: str | None, signature: str | None, body: bytes) -> None:
    """Verify the HMAC signature on a trigger request, raising HTTPException(401) on failure.

    Behavior:
    - No signature presented AND hmac NOT required → pass (backward-compatible Bearer-only).
    - No signature presented AND hmac IS required → 401 Missing trigger signature.
    - Signature presented (regardless of required flag) → always fully verify:
        * timestamp is a valid float
        * timestamp is within the configured replay window
        * HERMES_WEBHOOK_SECRET is present and non-empty
        * digest matches using constant-time comparison
    """
    from backend.config import get_settings
    s = get_settings()
    required = bool(getattr(s, "trigger_hmac_required", False))

    # No signature presented
    if not signature or not timestamp:
        if required:
            raise HTTPException(status_code=401, detail="Missing trigger signature")
        return  # backward-compatible: Bearer-only callers allowed

    # Signature presented → always verify (defense in depth even when not required)
    try:
        ts = float(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid signature timestamp")

    window = float(getattr(s, "trigger_hmac_window_s", 300))
    if abs(time.time() - ts) > window:
        raise HTTPException(status_code=401, detail="Signature timestamp outside window")

    try:
        secret = s.hermes_webhook_secret
    except Exception:
        secret = ""
    if not secret:
        raise HTTPException(status_code=401, detail="Trigger signing not configured")

    expected = compute_trigger_signature(secret, timestamp, body)
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Bad trigger signature")


@router.post("/api/trigger")  # full path — no prefix in include_router
async def hermes_trigger(body: TriggerRequest, request: Request, _=Depends(require_api_key)):
    # Order: Bearer auth (Depends) → HMAC verify → rate limit → dispatch.
    # Bearer-authenticated (Tier 1.6): Hermes presents the NEXUS_API_KEY.
    # HMAC (optional by default, required when trigger_hmac_required=True):
    #   Hermes signs with X-Timestamp + X-Signature headers using HERMES_WEBHOOK_SECRET.
    # Rate limiter (5/60s) caps abuse on top of auth.
    raw = await request.body()
    _verify_trigger_hmac(
        request.headers.get("X-Timestamp"),
        request.headers.get("X-Signature"),
        raw,
    )
    _check_rate_limit()
    known_tasks = {
        "briefing": _trigger_briefing,
        "status": _trigger_status,
    }
    fn = known_tasks.get(body.task_name)
    if not fn:
        raise HTTPException(status_code=404, detail=f"Unknown task: {body.task_name}")
    result = await fn(body.parameters)
    return {"ok": True, "result": result}


async def _trigger_briefing(params: dict) -> str:
    from backend.agents.briefing import run_briefing
    await run_briefing()
    return "briefing_triggered"


async def _trigger_status(params: dict) -> dict:
    from backend.integrations import homeassistant, unraid
    ha_ok = await homeassistant.health_check()
    ur_ok = await unraid.health_check()
    return {"ha": ha_ok, "unraid": ur_ok}
