from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlmodel import Session, or_, select

from backend.auth import require_api_key
from backend.database import AgentTrace, TraceSpan, get_session

router = APIRouter()


@router.get("")
async def list_traces(
    limit: int = 50,
    kind: str | None = None,
    q: str | None = None,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """Most-recent AgentTrace rows, newest first.

    `?limit=` defaults to 50, capped at 200. Optional `?kind=` filter
    (chat | briefing | orchestrator | proposer | voice). Optional `?q=`
    free-text filter, case-insensitive, matching the trace's own label OR
    any of its spans' input_summary/output_summary. Mirrors
    api/safety.py:list_actions (pure-read GET on a Depends-injected Session).

    Each row also carries span_count/total_cost_usd/total_tokens_in/
    total_tokens_out, aggregated from TraceSpan for the returned page —
    total_* are None (not 0) when the trace has no spans or every span's
    value was NULL, per this repo's None="unknown" vs 0="confirmed zero"
    convention (see unifi.alerts).
    """
    limit = max(1, min(limit, 200))
    stmt = select(AgentTrace)
    if kind is not None:
        stmt = stmt.where(AgentTrace.kind == kind)

    q = q.strip() if q else None
    if q:
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{esc}%"
        span_match = select(TraceSpan.trace_id).where(
            or_(
                TraceSpan.input_summary.ilike(pattern, escape="\\"),
                TraceSpan.output_summary.ilike(pattern, escape="\\"),
            )
        )
        stmt = stmt.where(
            or_(
                AgentTrace.label.ilike(pattern, escape="\\"),
                AgentTrace.id.in_(span_match),
            )
        )

    stmt = stmt.order_by(AgentTrace.started_at.desc()).limit(limit)
    rows = session.exec(stmt).all()

    ids = [r.id for r in rows]
    agg = {}
    if ids:
        agg_rows = session.exec(
            select(
                TraceSpan.trace_id,
                func.count(TraceSpan.id),
                func.sum(TraceSpan.cost_usd),
                func.sum(TraceSpan.tokens_in),
                func.sum(TraceSpan.tokens_out),
            ).where(TraceSpan.trace_id.in_(ids)).group_by(TraceSpan.trace_id)
        ).all()
        agg = {tid: (cnt, cost, tin, tout) for tid, cnt, cost, tin, tout in agg_rows}

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
            "span_count": agg.get(r.id, (0, None, None, None))[0],
            "total_cost_usd": agg.get(r.id, (0, None, None, None))[1],
            "total_tokens_in": agg.get(r.id, (0, None, None, None))[2],
            "total_tokens_out": agg.get(r.id, (0, None, None, None))[3],
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
