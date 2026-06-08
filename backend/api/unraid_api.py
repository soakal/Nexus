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
    ok = await restart_docker(container_id)
    if not ok:
        raise HTTPException(status_code=500, detail="Restart failed")
    return {"ok": True}
