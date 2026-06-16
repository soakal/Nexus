import asyncio

import pytest
from unittest.mock import AsyncMock, patch

from sqlmodel import SQLModel, Session, create_engine
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


def _seed_task(eng, prompt="task", status="pending"):
    from backend.database import Task

    with Session(eng) as s:
        t = Task(prompt=prompt, status=status)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


@pytest.mark.asyncio
async def test_pool_picks_up_pending_task(eng):
    """enqueue -> a worker pulls the id and runs run_task to completion."""
    from backend.agents.worker_pool import TaskWorkerPool

    task_id = _seed_task(eng)
    done = asyncio.Event()
    seen = {}

    async def fake_run_task(prompt, tid):
        seen["prompt"] = prompt
        seen["task_id"] = tid
        done.set()

    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task):
        await pool.start()
        await pool.enqueue(task_id)
        await asyncio.wait_for(done.wait(), timeout=5)
        await pool.stop()

    assert seen["task_id"] == task_id
    assert seen["prompt"] == "task"


@pytest.mark.asyncio
async def test_pool_bounded_concurrency(tmp_path, monkeypatch):
    """With size=2, at most 2 tasks run concurrently. Event-gated, no sleeps.

    Uses a FILE-based engine (not the StaticPool :memory: fixture): this test runs
    the pool with size=2, so two worker threads do concurrent Session(engine) reads.
    StaticPool shares ONE SQLite connection across threads, and concurrent use of a
    single connection intermittently raises a SQLAlchemy error under load (the source
    of a long-standing flake). A file DB hands each thread its own pooled connection,
    matching how production runs against the real WAL nexus.db.
    """
    from backend.agents.worker_pool import TaskWorkerPool

    db_path = tmp_path / "pool_concurrency.db"
    eng = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr("backend.database.engine", eng)

    ids = [_seed_task(eng, prompt=f"t{i}") for i in range(4)]

    active = 0
    max_active = 0
    release = asyncio.Event()
    started = asyncio.Event()
    lock = asyncio.Lock()

    async def fake_run_task(prompt, tid):
        nonlocal active, max_active
        async with lock:
            active += 1
            max_active = max(max_active, active)
            if active >= 2:
                started.set()
        await release.wait()
        async with lock:
            active -= 1

    pool = TaskWorkerPool(size=2)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task):
        await pool.start()
        for tid in ids:
            await pool.enqueue(tid)
        # Wait until 2 are concurrently in-flight, then confirm no 3rd starts.
        await asyncio.wait_for(started.wait(), timeout=5)
        # Give the loop a chance to (incorrectly) start a 3rd if unbounded.
        for _ in range(5):
            await asyncio.sleep(0)
        assert max_active == 2
        release.set()
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    assert max_active == 2


@pytest.mark.asyncio
async def test_request_cancel_sets_flag_and_stops(eng):
    """request_cancel sets Task.cancel_requested and hard-cancels an in-flight run."""
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    task_id = _seed_task(eng)
    running = asyncio.Event()
    cancelled = {"hit": False}

    async def fake_run_task(prompt, tid):
        running.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled["hit"] = True
            raise

    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task):
        await pool.start()
        await pool.enqueue(task_id)
        await asyncio.wait_for(running.wait(), timeout=5)
        await pool.request_cancel(task_id)
        # Let cancellation propagate.
        for _ in range(10):
            await asyncio.sleep(0)
            if cancelled["hit"]:
                break
        await pool.stop()

    assert cancelled["hit"] is True
    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.cancel_requested is True
