"""Knowledge graph REST. /health only at this commit; /topology lands later."""
import asyncio

from fastapi import APIRouter, Depends

from backend.auth import require_api_key
from backend.graph import enabled, store

router = APIRouter()


@router.get("/health")
async def graph_health(_=Depends(require_api_key)) -> dict:
    # 200 even when disabled; `jobs` carries last_success/last_error.
    return {"enabled": enabled(), **await asyncio.to_thread(store.health)}
