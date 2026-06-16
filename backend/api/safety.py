import asyncio
import json

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import ActionLog, TaskOutcome, get_session

router = APIRouter()


def _scheduler_running() -> bool:
    """Best-effort read of the scheduler's running flag. Guarded so the test
    fixture (which patches `scheduler` with running=False) and a not-yet-started
    scheduler both work without raising."""
    try:
        from backend.scheduler import scheduler
        return bool(getattr(scheduler, "running", False))
    except Exception:
        return False


def _parse_json(raw: str | None):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


@router.get("/actions")
async def list_actions(
    limit: int = 50,
    decision: str | None = None,
    actor: str | None = None,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """Most-recent ActionLog rows (immutable audit trail), newest first.

    `?limit=` defaults to 50, capped at 200. Optional `?decision=` and `?actor=`
    filters. Mirrors api/tasks.py:list_tasks (pure-read GET on a Depends-injected
    Session — established NEXUS pattern, no to_thread needed here).
    """
    limit = max(1, min(limit, 200))
    stmt = select(ActionLog)
    if decision is not None:
        stmt = stmt.where(ActionLog.decision == decision)
    if actor is not None:
        stmt = stmt.where(ActionLog.actor == actor)
    stmt = stmt.order_by(ActionLog.created_at.desc()).limit(limit)
    rows = session.exec(stmt).all()

    return [
        {
            "id": r.id,
            "actor": r.actor,
            "kind": r.kind,
            "target": r.target,
            "payload": _parse_json(r.payload_json),
            "risk": r.risk,
            "reversibility": r.reversibility,
            "decision": r.decision,
            "result": _parse_json(r.result_json),
            "idempotency_key": r.idempotency_key,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/outcomes")
async def list_outcomes(
    limit: int = 50,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """Recent Opus-verifier TaskOutcome rows (Tier 2.2 learning loop), newest first.

    `?limit=` defaults to 50, capped at 200. Mirrors list_actions (pure-read GET
    on a Depends-injected Session — no to_thread needed here).
    """
    limit = max(1, min(limit, 200))
    stmt = select(TaskOutcome).order_by(TaskOutcome.created_at.desc()).limit(limit)
    rows = session.exec(stmt).all()

    return [
        {
            "id": r.id,
            "task_id": r.task_id,
            "verdict": r.verdict,
            "confidence": r.confidence,
            "reason": r.reason,
            "grounded": r.grounded,
            "evidence": r.evidence,
            "model": r.model,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/hermes-actions")
async def list_hermes_actions(_=Depends(require_api_key)):
    """The structured Hermes verb allowlist (Tier 1.4 relay quarantine).

    Pure read of an in-memory allowlist — no DB, no to_thread. Returns a JSON-safe
    description of every verb (no callables) so a client can present the menu.
    """
    from backend.safety import hermes_actions

    return {"verbs": hermes_actions.allowed_verbs()}


@router.post("/actions/{action_id}/confirm")
async def confirm_action(
    action_id: int,
    _=Depends(require_api_key),
):
    """Confirm-and-dispatch a `needs_confirm` action (Tier 1.5 — Piece B).

    Re-checks the global kill switch and confirmation TTL at dispatch time.
    Only a row whose decision is exactly `needs_confirm` can be confirmed —
    everything else is rejected (default-deny posture). The existing ActionLog
    row is updated in place; no second row is created.

    Status codes:
      200  — dispatch attempted (status: executed | failed)
      403  — blocked by kill switch (autonomy paused)
      404  — action row not found
      409  — row exists but is not awaiting confirmation
      410  — confirmation window expired (TTL elapsed)
    """
    from backend.config import get_settings
    from backend.safety import broker

    ttl = get_settings().action_confirm_ttl_seconds
    status, res = await broker.confirm_action(action_id, ttl_seconds=ttl)

    if status == "not_found":
        raise HTTPException(status_code=404, detail="Action not found")
    if status == "not_confirmable":
        raise HTTPException(status_code=409, detail="Action is not awaiting confirmation")
    if status == "expired":
        raise HTTPException(status_code=410, detail="Confirmation window expired")
    if status == "forbidden":
        raise HTTPException(status_code=403, detail="Blocked: autonomy is paused (kill switch on)")

    # executed | failed — both return 200 with the dispatch outcome in the body
    return {
        "id": action_id,
        "status": status,
        "decision": res.decision.value if res else None,
        "result": res.result if res else None,
        "error": res.error if res else None,
    }


# ---------------------------------------------------------------------------
# Cost governor / kill switch (Tier 1.5)
# ---------------------------------------------------------------------------

@router.post("/pause")
async def pause_autonomy(_=Depends(require_api_key)):
    """Global kill switch ON: disable agent/autonomous side effects + pause the
    scheduler. User actions are unaffected."""
    from backend.safety import governor

    await asyncio.to_thread(governor.set_autonomy, False)
    try:
        from backend.scheduler import scheduler
        if getattr(scheduler, "running", False):
            scheduler.pause()
    except Exception:
        pass
    return {"autonomy_enabled": False, "scheduler_running": _scheduler_running()}


@router.post("/resume")
async def resume_autonomy(_=Depends(require_api_key)):
    """Global kill switch OFF: re-enable autonomy + resume the scheduler."""
    from backend.safety import governor

    await asyncio.to_thread(governor.set_autonomy, True)
    try:
        from backend.scheduler import scheduler
        if getattr(scheduler, "running", False):
            scheduler.resume()
    except Exception:
        pass
    return {"autonomy_enabled": True, "scheduler_running": _scheduler_running()}


@router.get("/status")
async def safety_status(_=Depends(require_api_key)):
    """Current kill-switch + budget state plus today's spend."""
    from backend.safety import governor

    state = await asyncio.to_thread(governor.get_system_state)
    spend = await asyncio.to_thread(governor.today_spend_usd)
    return {
        "autonomy_enabled": state["autonomy_enabled"],
        "today_spend_usd": spend,
        "daily_budget_usd": state["daily_budget_usd"],
        "per_task_budget_usd": state["per_task_budget_usd"],
        "scheduler_running": _scheduler_running(),
    }


@router.post("/budget")
async def set_budget(
    body: dict = Body(default_factory=dict),
    _=Depends(require_api_key),
):
    """Runtime cap-setter. Body: {daily_usd?: float, per_task_usd?: float}.
    Returns the new state."""
    from backend.safety import governor

    daily = body.get("daily_usd")
    per_task = body.get("per_task_usd")
    await asyncio.to_thread(
        governor.set_budgets,
        float(daily) if daily is not None else None,
        float(per_task) if per_task is not None else None,
    )
    state = await asyncio.to_thread(governor.get_system_state)
    spend = await asyncio.to_thread(governor.today_spend_usd)
    return {
        "autonomy_enabled": state["autonomy_enabled"],
        "today_spend_usd": spend,
        "daily_budget_usd": state["daily_budget_usd"],
        "per_task_budget_usd": state["per_task_budget_usd"],
        "scheduler_running": _scheduler_running(),
    }
