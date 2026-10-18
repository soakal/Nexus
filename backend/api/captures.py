"""Quill capture items. All routes require the NEXUS API key.

  GET   /api/captures/items?category=&status=&scope=today|open
  POST  /api/captures/items/{id}/done | reopen | snooze  ({"days": 1})
  PATCH /api/captures/items/{id}   (title, due_at, kind, category)
"""
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.agents import captures
from backend.auth import require_api_key

router = APIRouter(dependencies=[Depends(require_api_key)])


class SnoozeBody(BaseModel):
    days: int = Field(default=1, ge=1, le=365)


class ItemPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    due_at: datetime | None = None
    kind: Literal["task", "followup", "decision", "idea"] | None = None
    category: Literal["work", "personal"] | None = None


@router.get("/items")
async def list_items(category: str | None = None, status: str | None = None,
                     scope: Literal["today", "open"] | None = None):
    return await captures.list_items(category, status, scope)


async def _transition(item_id: int, status: str, days: int = 1) -> dict:
    res = await captures.set_status(item_id, status, "api", days)
    if res == "not_found":
        raise HTTPException(404, "not_found")
    if res != "ok":
        raise HTTPException(409, res)
    return {"id": item_id, "status": status, "done_by": "api" if status == "done" else None}


@router.post("/items/{item_id}/done")
async def done(item_id: int):
    return await _transition(item_id, "done")


@router.post("/items/{item_id}/reopen")
async def reopen(item_id: int):
    return await _transition(item_id, "open")


@router.post("/items/{item_id}/snooze")
async def snooze(item_id: int, body: SnoozeBody | None = None):
    return await _transition(item_id, "snoozed", (body or SnoozeBody()).days)


@router.patch("/items/{item_id}")
async def patch(item_id: int, body: ItemPatch):
    fields = {k: getattr(body, k) for k in body.model_fields_set}
    if "title" in fields and fields["title"] is None:
        raise HTTPException(422, "title cannot be null")
    if fields.get("kind", 0) is None or fields.get("category", 0) is None:
        raise HTTPException(422, "kind/category cannot be null")
    row = await captures.patch_item(item_id, fields)
    if row is None:
        raise HTTPException(404, "not_found")
    return row
