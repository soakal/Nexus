"""Goals API — propose / list / inspect / approve / reject.

All endpoints are Bearer-auth gated (require_api_key).
DB work is delegated to backend.agents.goals which wraps all sync DB helpers
in asyncio.to_thread; nothing here touches a Session directly.
"""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from backend.auth import require_api_key
from backend.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()


class GoalPropose(BaseModel):
    title: str
    description: str
    actor: str = "user"
    confidence: float = 0.6
    risk: str = "medium"
    reversibility: str = "unknown"


class GoalReject(BaseModel):
    reason: str | None = None


@router.post("/propose")
async def propose_goal(body: GoalPropose, _=Depends(require_api_key)):
    from backend.agents import goals

    s = get_settings()
    result = await goals.propose(
        body.title,
        body.description,
        actor=body.actor,
        confidence=body.confidence,
        risk=body.risk,
        reversibility=body.reversibility,
        ttl_seconds=s.goal_ttl_seconds,
        debounce_seconds=s.goal_debounce_seconds,
    )
    return result


@router.get("/")
async def list_goals(status: str | None = None, _=Depends(require_api_key)):
    from backend.agents import goals

    s = get_settings()
    await goals.reconcile_running(
        backoff_base_seconds=s.goal_backoff_base_seconds,
        max_attempts=s.goal_max_attempts,
    )
    return await asyncio.to_thread(goals._db_list_goals, status, 100)


@router.get("/{goal_id}")
async def get_goal(goal_id: int, _=Depends(require_api_key)):
    from backend.agents import goals

    g = await asyncio.to_thread(goals._db_get_goal, goal_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return g


@router.post("/{goal_id}/approve")
async def approve_goal(goal_id: int, _=Depends(require_api_key)):
    from backend.agents import goals

    r = await goals.approve(goal_id)
    if r["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Goal not found")
    if r["status"] == "conflict":
        raise HTTPException(status_code=409, detail=f"Goal is already {r['current']}")
    if r["status"] == "expired":
        raise HTTPException(status_code=410, detail="Goal proposal has expired")
    return r


@router.post("/{goal_id}/reject")
async def reject_goal(goal_id: int, body: GoalReject = GoalReject(), _=Depends(require_api_key)):
    from backend.agents import goals

    r = await goals.reject(goal_id, reason=body.reason)
    if r["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Goal not found")
    if r["status"] == "conflict":
        raise HTTPException(status_code=409, detail=f"Goal cannot be rejected from status {r['current']}")
    return r
