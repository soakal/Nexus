from fastapi import APIRouter, Depends, HTTPException

from backend.auth import require_api_key

router = APIRouter()


@router.get("/")
async def get_unraid_data(_=Depends(require_api_key)):
    from backend.integrations.unraid import fetch
    return await fetch()


@router.post("/docker/{container_id}/restart")
async def restart_container(container_id: str, _=Depends(require_api_key)):
    from backend.integrations.unraid import restart_docker
    result = await restart_docker(container_id)
    if not result.get("success"):
        detail = result.get("error", "Restart failed")
        if result.get("stopped"):
            detail = f"Container STOPPED but failed to restart: {detail}"
        raise HTTPException(status_code=500, detail=detail)
    return {"ok": True}
