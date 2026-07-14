from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import Task, TaskStep, get_session

router = APIRouter()


class TaskCreate(BaseModel):
    prompt: str


@router.post("/")
async def create_task(body: TaskCreate, _=Depends(require_api_key), session: Session = Depends(get_session)):
    task = Task(prompt=body.prompt, status="pending")
    session.add(task)
    session.commit()
    session.refresh(task)

    from backend.agents.worker_pool import get_pool
    await get_pool().enqueue(task.id)
    return {"id": task.id, "status": "pending"}


@router.get("/")
async def list_tasks(_=Depends(require_api_key), session: Session = Depends(get_session)):
    tasks = session.exec(select(Task).order_by(Task.created_at.desc()).limit(50)).all()
    return tasks


@router.delete("/{task_id}")
async def cancel_task(task_id: int, _=Depends(require_api_key), session: Session = Depends(get_session)):
    """Cooperatively stop a running task, then delete it from the database."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Set cancel_requested + hard-cancel the in-flight coroutine BEFORE deleting
    # the row. A missing row downstream is a no-op for the worker.
    from backend.agents.worker_pool import get_pool
    await get_pool().request_cancel(task_id)

    session.delete(task)
    session.commit()
    return {"id": task_id, "status": "cancelled"}


@router.post("/{task_id}/retry")
async def retry_task(task_id: int, _=Depends(require_api_key), session: Session = Depends(get_session)):
    """Re-run a task with its original prompt, from scratch."""
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Guard against double-enqueue using durable Task.status (the dead _running
    # dict is gone). Already-active tasks can't be retried.
    if task.status in ("pending", "running"):
        raise HTTPException(status_code=409, detail="Task is already running")

    # Wipe prior durable progress so the rerun re-plans + re-executes cleanly.
    for step in session.exec(select(TaskStep).where(TaskStep.task_id == task_id)).all():
        session.delete(step)

    task.status = "pending"
    task.result_json = None
    task.plan_json = None
    task.steps_taken = 0
    task.cancel_requested = False
    session.add(task)
    session.commit()
    session.refresh(task)

    from backend.agents.worker_pool import get_pool
    await get_pool().enqueue(task.id)
    return {"id": task.id, "status": "pending"}
