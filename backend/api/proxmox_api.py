from fastapi import APIRouter, Depends

from backend.auth import require_api_key

router = APIRouter()


@router.get("/")
async def get_proxmox_data(_=Depends(require_api_key)):
    from backend.integrations.proxmox import fetch
    return await fetch()
