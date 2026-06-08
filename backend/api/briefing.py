from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import Briefing, get_session

router = APIRouter()


@router.post("/trigger")
async def trigger_briefing(_=Depends(require_api_key)):
    from backend.agents.briefing import run_briefing
    briefing_text = await run_briefing()
    return {"status": "ok", "briefing": briefing_text}


@router.get("/latest")
async def get_latest_briefing(session: Session = Depends(get_session)):
    # No auth required — Hermes needs this
    b = session.exec(select(Briefing).order_by(Briefing.created_at.desc()).limit(1)).first()
    if not b:
        raise HTTPException(status_code=404, detail="No briefings yet")
    return b


@router.get("/")
async def list_briefings(_=Depends(require_api_key), session: Session = Depends(get_session)):
    briefings = session.exec(select(Briefing).order_by(Briefing.created_at.desc()).limit(20)).all()
    return briefings
