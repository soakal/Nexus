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


@router.post("/docker/prune")
async def prune_images(_=Depends(require_api_key)):
    """Prune dangling Docker images on Unraid (broker-gated, Phase 7d).

    Native SSH, not via Hermes's relay — see backend/safety/broker.py's
    unraid_docker_prune dispatcher and backend/integrations/unraid.py's
    prune_docker_images. Unlike restart_container above, this route goes
    through the broker (actor="user") rather than dispatching directly.
    """
    from backend.safety.broker import Decision, execute_action

    res = await execute_action(
        actor="user",
        kind="unraid_docker_prune",
        target="unraid",
        payload={},
    )
    if res.decision == Decision.EXECUTED:
        return {"ok": True, "result": res.result}
    raise HTTPException(
        status_code=502,
        detail=f"Docker prune failed: {res.error or res.decision.value}",
    )
