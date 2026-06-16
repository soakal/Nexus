import json

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import ActionLog, get_session

router = APIRouter()


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


@router.post("/actions/{action_id}/confirm")
async def confirm_action(
    action_id: int,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """Confirm a `needs_confirm` action.

    Documented inert placeholder: validation is real (404 for a missing row, 409
    for a row that is not awaiting confirmation), but no dispatch happens yet. The
    confirm-and-dispatch flow lands with Tier 1.5 autonomy, when agents/autonomous
    actors actually generate `needs_confirm` rows that a human approves. Until then
    no production path produces a confirmable row, so this safely returns a
    not-yet-wired marker rather than dispatching.
    """
    row = session.get(ActionLog, action_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Action not found")
    if row.decision != "needs_confirm":
        raise HTTPException(status_code=409, detail="Action is not awaiting confirmation")
    return {
        "id": action_id,
        "status": "confirm_not_yet_wired",
        "detail": "Confirm flow lands with Tier 1.5 autonomy",
    }
