from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import AgentTrace, TraceSpan, get_session

router = APIRouter()


@router.get("")
async def list_traces(
    limit: int = 50,
    kind: str | None = None,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """Most-recent AgentTrace rows, newest first.

    `?limit=` defaults to 50, capped at 200. Optional `?kind=` filter
    (chat | briefing | orchestrator | proposer | voice). Mirrors
    api/safety.py:list_actions (pure-read GET on a Depends-injected Session).
    """
    limit = max(1, min(limit, 200))
    stmt = select(AgentTrace)
    if kind is not None:
        stmt = stmt.where(AgentTrace.kind == kind)
    stmt = stmt.order_by(AgentTrace.started_at.desc()).limit(limit)
    rows = session.exec(stmt).all()

    return [
        {
            "id": r.id,
            "kind": r.kind,
            "label": r.label,
            "task_id": r.task_id,
            "started_at": r.started_at.isoformat(),
            "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            "status": r.status,
            "error": r.error,
        }
        for r in rows
    ]


@router.get("/{trace_id}")
async def get_trace(
    trace_id: int,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """A single AgentTrace plus its TraceSpan rows, ordered by started_at.

    404s when the trace does not exist.
    """
    trace = session.get(AgentTrace, trace_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Trace not found")

    stmt = select(TraceSpan).where(TraceSpan.trace_id == trace_id).order_by(TraceSpan.started_at)
    spans = session.exec(stmt).all()

    return {
        "id": trace.id,
        "kind": trace.kind,
        "label": trace.label,
        "task_id": trace.task_id,
        "started_at": trace.started_at.isoformat(),
        "ended_at": trace.ended_at.isoformat() if trace.ended_at else None,
        "status": trace.status,
        "error": trace.error,
        "spans": [
            {
                "id": s.id,
                "trace_id": s.trace_id,
                "parent_span_id": s.parent_span_id,
                "span_type": s.span_type,
                "name": s.name,
                "started_at": s.started_at.isoformat(),
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "duration_ms": s.duration_ms,
                "input_summary": s.input_summary,
                "output_summary": s.output_summary,
                "tokens_in": s.tokens_in,
                "tokens_out": s.tokens_out,
                "cost_usd": s.cost_usd,
                "error": s.error,
            }
            for s in spans
        ],
    }
