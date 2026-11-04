"""Knowledge graph REST: /health, /topology, /changes."""
import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.auth import require_api_key
from backend.graph import enabled, now_iso, store, to_utc_iso

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


_COLLECTOR_SOURCES = frozenset({"proxmox_api", "unraid_api", "uptime_targets", "manual", "vault", "wikilink", "frontmatter"})


def _resolve_one(ref: str) -> str:
    found = store.resolve_node(ref)
    if not found:
        raise HTTPException(404, f"unknown node: {ref}")
    if len(found) > 1:
        raise HTTPException(409, {"detail": "ambiguous", "candidates": [n["id"] for n in found]})
    return found[0]["id"]


@router.get("/changes")
async def graph_changes(
    node: str,
    since: str = "24h",
    ancestors: bool = True,
    limit: int = Query(100, ge=1, le=500),
    _=Depends(require_api_key),
) -> dict:
    if not enabled():
        raise HTTPException(503, "graph disabled")
    try:
        since_utc = to_utc_iso(since)
    except ValueError as e:
        raise HTTPException(422, f"bad since: {e}") from e
    nid = await asyncio.to_thread(_resolve_one, node)
    ids = [nid] + ([i for i, _ in await asyncio.to_thread(store.ancestors, nid, 3)] if ancestors else [])
    rows = await asyncio.to_thread(lambda: store.changes_for(ids, since=since_utc, limit=limit))
    return {"node": nid, "since": since_utc, "nodes": ids, "changes": rows}


class ChangeIn(BaseModel):
    node_id: str | None = Field(None, max_length=200)
    machine: str | None = Field(None, max_length=200)
    field: str = Field(pattern=r"^[a-z0-9_.:-]{1,64}$")
    old: str | None = Field(None, max_length=500)
    new: str | None = Field(None, max_length=500)
    source: str = Field(pattern=r"^[a-z0-9_-]{1,32}$")
    ts: str | None = Field(None, max_length=64)


@router.post("/changes", status_code=201)
async def graph_record_change(body: ChangeIn, _=Depends(require_api_key)) -> dict:
    """An outside actor (e.g. a Kithos install) records a change on an EXISTING node. Never creates nodes."""
    if not enabled():
        raise HTTPException(503, "graph disabled")
    if (body.node_id is None) == (body.machine is None):
        raise HTTPException(422, "give exactly one of node_id or machine")
    if body.source in _COLLECTOR_SOURCES:
        raise HTTPException(422, f"source {body.source!r} is reserved for the collectors")
    try:
        ts = to_utc_iso(body.ts) if body.ts else now_iso()
    except ValueError as e:
        raise HTTPException(422, f"bad ts: {e}") from e
    if body.node_id is not None:
        if await asyncio.to_thread(store.get_node, body.node_id) is None:
            raise HTTPException(404, f"unknown node: {body.node_id}")
        nid = body.node_id
    else:
        nid = await asyncio.to_thread(_resolve_one, body.machine)
    cid = await asyncio.to_thread(store.insert_change, ts, nid, body.field, body.old, body.new, body.source)
    return {"id": cid, "node_id": nid}
