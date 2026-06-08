import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import Task, get_session

router = APIRouter()

# Registry of in-flight asyncio tasks keyed by Task.id so they can be cancelled.
_running: dict[int, asyncio.Task] = {}


class TaskCreate(BaseModel):
    prompt: str


def _launch(prompt: str, task_id: int) -> None:
    """Fire-and-forget the orchestrator, tracking the asyncio task for cancellation."""

    async def _run():
        from backend.agents.orchestrator import run_task
        try:
            await run_task(prompt, task_id)
        finally:
            _running.pop(task_id, None)

    _running[task_id] = asyncio.create_task(_run())


@router.post("/")
async def create_task(body: TaskCreate, _=Depends(require_api_key), session: Session = Depends(get_session)):
    task = Task(prompt=body.prompt)
    session.add(task)
    session.commit()
    session.refresh(task)

    _launch(body.prompt, task.id)
    return {"id": task.id, "status": "running"}


@router.get("/")
async def list_tasks(_=Depends(require_api_key), session: Session = Depends(get_session)):
    tasks = session.exec(select(Task).order_by(Task.created_at.desc()).limit(50)).all()
    return tasks


@router.get("/{task_id}")
async def get_task(task_id: int, _=Depends(require_api_key), session: Session = Depends(get_session)):
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.delete("/{task_id}")
async def cancel_task(task_id: int, _=Depends(require_api_key), session: Session = Depends(get_session)):
    """Cancel a running task (if in flight) and delete it from the database."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    running = _running.pop(task_id, None)
    if running and not running.done():
        running.cancel()

    session.delete(task)
    session.commit()
    return {"id": task_id, "status": "cancelled"}


@router.post("/{task_id}/retry")
async def retry_task(task_id: int, _=Depends(require_api_key), session: Session = Depends(get_session)):
    """Re-run a task with its original prompt."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # If it's somehow still running, don't double-launch.
    existing = _running.get(task_id)
    if existing and not existing.done():
        raise HTTPException(status_code=409, detail="Task is already running")

    # Reset state for the rerun.
    task.status = "running"
    task.result_json = None
    task.plan_json = None
    task.steps_taken = 0
    session.add(task)
    session.commit()
    session.refresh(task)

    _launch(task.prompt, task.id)
    return {"id": task.id, "status": "running"}
