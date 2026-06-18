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
    cadence: str | None = None
    category: str | None = None
    success_criteria: str | None = None


class GoalReject(BaseModel):
    reason: str | None = None


class GoalEdit(BaseModel):
    title: str | None = None
    description: str | None = None
    risk: str | None = None
    category: str | None = None
    cadence: str | None = None
    success_criteria: str | None = None


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
        cadence=body.cadence,
        category=body.category,
        success_criteria=body.success_criteria,
    )
    return result


@router.get("/categories")
async def list_categories(_=Depends(require_api_key)):
    from backend.agents.goals import GOAL_CATEGORIES
    return {"categories": GOAL_CATEGORIES}


@router.get("/")
async def list_goals(
    status: str | None = None,
    category: str | None = None,
    _=Depends(require_api_key),
):
    from backend.agents import goals

    s = get_settings()
    await goals.reconcile_running(
        backoff_base_seconds=s.goal_backoff_base_seconds,
        max_attempts=s.goal_max_attempts,
    )
    all_goals = await asyncio.to_thread(goals._db_list_goals, status, 100)
    if category is not None:
        normalized = goals.normalize_category(category)
        all_goals = [g for g in all_goals if g.get("category") == normalized]
    return all_goals


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


@router.patch("/{goal_id}")
async def edit_goal(goal_id: int, body: GoalEdit, _=Depends(require_api_key)):
    """Edit a goal's editable fields from any status. Editing an already-run goal
    does not re-run it — see goals.edit()."""
    from backend.agents import goals

    r = await goals.edit(goal_id, body.model_dump(exclude_unset=True))
    if r["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Goal not found")
    if r["status"] == "conflict":
        cur = r.get("current")
        if cur in ("title_required", "description_required", "invalid_risk"):
            raise HTTPException(status_code=422, detail=cur)
        raise HTTPException(status_code=409, detail=f"Goal cannot be edited ({cur})")
    return r


@router.delete("/{goal_id}")
async def delete_goal(goal_id: int, _=Depends(require_api_key)):
    """Hard-delete a goal row (human cleanup). Allowed from any status."""
    from backend.agents import goals

    r = await goals.delete(goal_id)
    if r["status"] == "not_found":
        raise HTTPException(status_code=404, detail="Goal not found")
    return r
