import time

from fastapi import APIRouter, Depends, HTTPException
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


@router.post("/api/trigger")  # full path — no prefix in include_router
async def hermes_trigger(body: TriggerRequest, _=Depends(require_api_key)):
    # Bearer-authenticated (Tier 1.6): Hermes presents the NEXUS_API_KEY. A
    # process-local rate limiter (5/60s) caps abuse on top of auth.
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
