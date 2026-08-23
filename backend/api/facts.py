"""Fact audit / recall API (Tier 2.3c extension).

Endpoints:
  GET  /api/facts/                       — list active facts with effective_confidence
  POST /api/facts/                       — manually add an owner-asserted fact
  GET  /api/facts/recall?query=<str>     — show what a query would surface from recall
  POST /api/facts/{fact_id}/dismiss      — soft-dismiss a fact (non-destructive)
  POST /api/facts/{fact_id}/pin          — pin or unpin a fact (skip decay)

All endpoints require Bearer auth.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.auth import require_api_key

router = APIRouter()


class FactPin(BaseModel):
    pinned: bool = True


class FactCreate(BaseModel):
    subject: str
    predicate: str
    value: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


@router.get("/")
async def list_facts(_: str = Depends(require_api_key)):
    """Return all active facts with their effective confidence and floor status."""
    from backend.agents import facts
    return await facts.list_facts_for_audit()


@router.post("/")
async def create_fact(body: FactCreate, _: str = Depends(require_api_key)):
    """Manually add an owner-asserted fact (source='manual', confidence 1.0).

    Reinforces or supersedes an existing (subject, predicate) rather than
    duplicating it. Returns 400 if any field is blank/whitespace-only.
    """
    from backend.agents import facts

    subject = body.subject.strip()
    predicate = body.predicate.strip()
    value = body.value.strip()
    if not subject or not predicate or not value:
        raise HTTPException(status_code=400, detail="subject, predicate and value are all required")

    await facts.add_fact(subject, predicate, value, confidence=body.confidence)
    return {"subject": subject, "predicate": predicate, "value": value, "source": "manual"}


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


@router.post("/{fact_id}/pin")
async def pin_fact(fact_id: int, body: FactPin, _: str = Depends(require_api_key)):
    """Pin or unpin a fact. A pinned fact skips confidence decay entirely and is
    never superseded by an inferred extraction.

    Returns 404 if the fact does not exist.
    """
    from backend.agents import facts
    updated = await facts.set_pinned(fact_id, body.pinned)
    if not updated:
        raise HTTPException(status_code=404, detail=f"Fact {fact_id} not found")
    return {"id": fact_id, "pinned": body.pinned}
