from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.auth import require_api_key

router = APIRouter()


class FilterToggle(BaseModel):
    enabled: bool


class TimedDisable(BaseModel):
    minutes: int = 5


@router.get("/")
async def get_adguard_data(_=Depends(require_api_key)):
    from backend.integrations.adguard import fetch
    return await fetch()


@router.post("/filter")
async def set_filter(body: FilterToggle, _=Depends(require_api_key)):
    from backend.integrations.adguard import set_filtering
    await set_filtering(body.enabled)
    return {"ok": True, "enabled": body.enabled}


@router.post("/disable-timed")
async def timed_disable(body: TimedDisable, _=Depends(require_api_key)):
    from backend.integrations.adguard import disable_for_minutes
    await disable_for_minutes(body.minutes)
    return {"ok": True, "disabled_for_minutes": body.minutes}
