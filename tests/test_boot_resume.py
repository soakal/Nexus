import asyncio
import json

import pytest
from unittest.mock import AsyncMock, patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401  (register models before create_all)


def make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def eng(monkeypatch):
    e = make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _seed_task(eng, prompt="task", status="running"):
    from backend.database import Task

    with Session(eng) as s:
        t = Task(prompt=prompt, status=status)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


def _seed_step(eng, task_id, index, prompt, status="pending", output=None):
    from backend.agents.orchestrator import _idem_key
    from backend.database import TaskStep

    with Session(eng) as s:
        s.add(TaskStep(
            task_id=task_id,
            step_index=index,
            prompt=prompt,
            description=f"s{index}",
            status=status,
            output_json=json.dumps(output) if output is not None else None,
            idempotency_key=_idem_key(task_id, index, prompt),
        ))
        s.commit()


@pytest.mark.asyncio
async def test_boot_requeues_running_tasks(eng):
    """A Task left 'running' with [done, pending] resumes to success on boot.

    The worker loop does `from backend.agents.orchestrator import run_task`, so
    the REAL durable orchestrator runs; only the executor's tool-use loop
    (router.run_with_tools, Tier 2.1) is mocked.
    """
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    task_id = _seed_task(eng, status="running")
    _seed_step(eng, task_id, 1, "a", status="done", output="A")
    _seed_step(eng, task_id, 2, "b", status="pending")

    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = "B"
        await pool.start()  # start() calls requeue_unfinished()
        # Deterministic wait: the worker calls _queue.task_done() in its finally,
        # so join() returns the instant the re-enqueued task is fully processed.
        # (A fixed-time poll flaked under load once the Tier 1.6 per-step gates
        # added asyncio.to_thread hops ahead of execution.)
        await asyncio.wait_for(pool._queue.join(), timeout=15)
        await pool.stop()

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "success"
        assert json.loads(t.result_json) == ["A", "B"]
    # Only the pending step ran.
    assert mock_exec.await_count == 1


@pytest.mark.asyncio
async def test_boot_resets_orphan_running_step(eng):
    """A TaskStep stuck in 'running' (process died mid-step) is reset to pending
    by _load_steps and then executed."""
    task_id = _seed_task(eng, status="running")
    _seed_step(eng, task_id, 1, "orphan", status="running")  # died mid-flight

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = "RECOVERED"
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is True
    assert mock_exec.await_count == 1

    from backend.database import TaskStep

    with Session(eng) as s:
        step = s.exec(select(TaskStep).where(TaskStep.task_id == task_id)).first()
        assert step.status == "done"
        assert json.loads(step.output_json) == "RECOVERED"


@pytest.mark.asyncio
async def test_boot_does_not_force_fail(eng):
    """requeue_unfinished must NOT mark running/pending tasks as failed — it
    re-enqueues them, leaving status untouched until a worker runs them."""
    from backend.agents.worker_pool import TaskWorkerPool

    running_id = _seed_task(eng, status="running")
    pending_id = _seed_task(eng, status="pending")

    pool = TaskWorkerPool(size=0)  # no workers -> nothing drains the queue
    await pool.requeue_unfinished()

    from backend.database import Task

    with Session(eng) as s:
        assert s.get(Task, running_id).status == "running"
        assert s.get(Task, pending_id).status == "pending"
    # Both ids were placed on the queue.
    assert pool._queue.qsize() == 2
