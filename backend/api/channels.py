from fastapi import APIRouter, Depends, HTTPException

from backend.auth import require_api_key

router = APIRouter()


@router.get("/")
async def get_channels_data(_=Depends(require_api_key)):
    from backend.integrations.channels_dvr import fetch
    data = await fetch()
    return data


@router.post("/record")
async def trigger_recording(body: dict, _=Depends(require_api_key)):
    from backend.integrations.channels_dvr import trigger_recording
    program_id = body.get("program_id", "")
    if not program_id:
        raise HTTPException(status_code=400, detail="program_id required")
    try:
        result = await trigger_recording(program_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
