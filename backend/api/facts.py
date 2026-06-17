"""Fact audit / recall API (Tier 2.3c extension).

Endpoints:
  GET  /api/facts/                       — list active facts with effective_confidence
  GET  /api/facts/recall?query=<str>     — show what a query would surface from recall
  POST /api/facts/{fact_id}/dismiss      — soft-dismiss a fact (non-destructive)

All endpoints require Bearer auth.
"""

from fastapi import APIRouter, Depends, HTTPException

from backend.auth import require_api_key

router = APIRouter()


@router.get("/")
async def list_facts(_: str = Depends(require_api_key)):
    """Return all active facts with their effective confidence and floor status."""
    from backend.agents import facts
    return await facts.list_facts_for_audit()


@router.get("/recall")
async def recall_facts(query: str, _: str = Depends(require_api_key)):
    """Return the formatted recall string that a given query would surface."""
    from backend.agents import facts
    result = await facts.facts_recall(query)
    return {"query": query, "result": result}


@router.post("/{fact_id}/dismiss")
async def dismiss_fact(fact_id: int, _: str = Depends(require_api_key)):
    """Soft-dismiss a fact by id. The row is preserved but excluded from recall.

    Returns 404 if the fact does not exist.
    """
    from backend.agents import facts
    dismissed = await facts.dismiss_fact(fact_id)
    if not dismissed:
        raise HTTPException(status_code=404, detail=f"Fact {fact_id} not found")
    return {"id": fact_id, "dismissed": True}
