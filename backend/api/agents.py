
from fastapi import APIRouter, Depends, WebSocket
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import AgentRun, get_session

router = APIRouter()


class WebSocketManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket, subprotocol: str | None = None):
        # Echo the negotiated subprotocol so the browser handshake completes when
        # the client offered one (used to pass the API key out of the URL).
        await ws.accept(subprotocol=subprotocol)
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: str):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = WebSocketManager()


@router.get("/runs")
async def list_runs(
    q: str = "",
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    runs = session.exec(select(AgentRun).order_by(AgentRun.created_at.desc()).limit(100)).all()
    if q:
        runs = [r for r in runs if q.lower() in (r.prompt_snippet + r.output_snippet).lower()]
    return runs


