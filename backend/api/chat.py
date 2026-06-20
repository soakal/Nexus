import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import ChatMessage, Conversation, get_session

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    conversation_id: int | None = None


@router.post("/")
async def send_message(
    body: ChatRequest,
    _=Depends(require_api_key),
):
    from backend.agents.chat import chat
    result = await chat(body.conversation_id, body.message)
    return result


@router.post("/stream")
async def stream_message(body: ChatRequest, _=Depends(require_api_key)):
    """SSE stream: CHAT intent yields tokens; other intents yield a single done event."""
    from backend.agents.chat import chat

    q: asyncio.Queue = asyncio.Queue()

    async def event_gen():
        task = asyncio.create_task(chat(body.conversation_id, body.message, token_queue=q))
        while True:
            token = await q.get()
            if token is None:
                break
            yield f"data: {json.dumps({'type': 'token', 'text': token})}\n\n"
        result = await task
        yield f"data: {json.dumps({'type': 'done', 'conversation_id': result['conversation_id'], 'reply': result['reply']})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/conversations")
async def list_conversations(
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    convs = session.exec(
        select(Conversation)
        .order_by(Conversation.updated_at.desc())
        .limit(50)
    ).all()
    return [
        {"id": c.id, "title": c.title, "updated_at": c.updated_at.isoformat()}
        for c in convs
    ]


@router.get("/{conversation_id}")
async def get_conversation(
    conversation_id: int,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    conv = session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = session.exec(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ChatMessage.created_at)
    ).all()

    return {
        "id": conv.id,
        "title": conv.title,
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
            for m in msgs
        ],
    }


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: int,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    conv = session.get(Conversation, conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    def _delete():
        from sqlmodel import Session as SyncSession
        from backend.database import engine
        with SyncSession(engine) as s:
            msgs = s.exec(
                select(ChatMessage).where(ChatMessage.conversation_id == conversation_id)
            ).all()
            for m in msgs:
                s.delete(m)
            c = s.get(Conversation, conversation_id)
            if c:
                s.delete(c)
            s.commit()

    await asyncio.to_thread(_delete)
    return {"ok": True}
