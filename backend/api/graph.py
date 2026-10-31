"""Knowledge graph REST: /health and /topology."""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import require_api_key
from backend.graph import enabled, store

router = APIRouter()


@router.get("/health")
async def graph_health(_=Depends(require_api_key)) -> dict:
    # 200 even when disabled; `jobs` carries last_success/last_error.
    return {"enabled": enabled(), **await asyncio.to_thread(store.health)}


@router.get("/topology")
async def graph_topology(
    node: str, direction: Literal["up", "down", "both"] = "both", depth: int = 3, _=Depends(require_api_key)
) -> dict:
    from backend.graph import topology

    if not enabled():
        raise HTTPException(503, "graph disabled")
    found = await asyncio.to_thread(store.resolve_node, node)
    if not found:
        raise HTTPException(404, f"unknown node: {node}")
    if len(found) > 1:
        raise HTTPException(409, {"detail": "ambiguous", "candidates": [n["id"] for n in found]})
    return await topology.describe(found[0]["id"], direction, max(1, min(3, depth)))
