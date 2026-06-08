from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class TriggerRequest(BaseModel):
    task_name: str
    parameters: dict = {}


@router.post("/api/trigger")  # full path — no prefix in include_router
async def hermes_trigger(body: TriggerRequest):
    # Hermes can call this without auth using shared secret verified at network level
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
