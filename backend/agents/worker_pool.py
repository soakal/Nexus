"""Bounded async worker pool that drains pending/running Tasks from the DB.

The pool is the single owner of orchestration concurrency: `create_task`
enqueues a Task id, N worker coroutines pull ids off an `asyncio.Queue` and run
`run_task(prompt, task_id)` for each. On boot, `requeue_unfinished()` re-enqueues
every Task left in a non-terminal state so work resumes rather than being
force-failed.

All synchronous SQLite access happens inside `asyncio.to_thread` helpers so the
event loop is never blocked (Windows ProactorEventLoop safety — see CLAUDE.md).
"""
import asyncio
import logging
import os

logger = logging.getLogger(__name__)


def _default_size() -> int:
    try:
        return max(1, int(os.environ.get("NEXUS_TASK_WORKERS", "2")))
    except (TypeError, ValueError):
        return 2


# --- sync DB helpers (run via asyncio.to_thread) ---------------------------

def _set_task_pending(task_id: int) -> None:
    from datetime import datetime

    from sqlmodel import Session

    from backend.database import Task, engine

    with Session(engine) as session:
        t = session.get(Task, task_id)
        if t:
            t.status = "pending"
            t.cancel_requested = False
            t.updated_at = datetime.utcnow()
            session.commit()


def _set_cancel_requested(task_id: int) -> None:
    from datetime import datetime

    from sqlmodel import Session

    from backend.database import Task, engine

    with Session(engine) as session:
        t = session.get(Task, task_id)
        if t:
            t.cancel_requested = True
            t.updated_at = datetime.utcnow()
            session.commit()


def _load_task_prompt(task_id: int) -> str | None:
    from sqlmodel import Session

    from backend.database import Task, engine

    with Session(engine) as session:
        t = session.get(Task, task_id)
        return t.prompt if t else None


def _load_unfinished_task_ids() -> list[int]:
    """Ids of every Task left in a non-terminal state (pending|running)."""
    from sqlmodel import Session, select

    from backend.database import Task, engine

    with Session(engine) as session:
        rows = session.exec(
            select(Task).where(Task.status.in_(["pending", "running"]))
        ).all()
        return [t.id for t in rows if t.id is not None]


def _finalize_failed(task_id: int, reason: str) -> None:
    import json
    from datetime import datetime

    from sqlmodel import Session

    from backend.database import Task, engine

    with Session(engine) as session:
        t = session.get(Task, task_id)
        if t:
            t.status = "failed"
            t.result_json = json.dumps({"error": reason})
            t.updated_at = datetime.utcnow()
            session.commit()


# --- pool ------------------------------------------------------------------

class TaskWorkerPool:
    def __init__(self, size: int | None = None):
        self.size = size if size is not None else _default_size()
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._inflight: dict[int, asyncio.Task] = {}
        self._started = False
        self._stopping = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self.size):
            self._workers.append(asyncio.create_task(self._worker_loop(i)))
        await self.requeue_unfinished()

    async def stop(self) -> None:
        self._stopping = True
        # Cancel any in-flight orchestrations first, then the worker coroutines.
        for handle in list(self._inflight.values()):
            if not handle.done():
                handle.cancel()
        for w in self._workers:
            w.cancel()
        if self._workers:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._workers, return_exceptions=True), timeout=10
                )
            except asyncio.TimeoutError:
                pass
            except Exception:  # noqa: BLE001
                pass
        self._workers.clear()
        self._inflight.clear()
        self._started = False

    async def enqueue(self, task_id: int) -> None:
        await asyncio.to_thread(_set_task_pending, task_id)
        await self._queue.put(task_id)

    async def request_cancel(self, task_id: int) -> None:
        await asyncio.to_thread(_set_cancel_requested, task_id)
        # Hard backstop: if it is actively running, cancel the coroutine too.
        handle = self._inflight.get(task_id)
        if handle and not handle.done():
            handle.cancel()

    async def requeue_unfinished(self) -> None:
        ids = await asyncio.to_thread(_load_unfinished_task_ids)
        for task_id in ids:
            await self._queue.put(task_id)
        if ids:
            logger.info(f"Re-enqueued {len(ids)} unfinished task(s) on boot")

    async def _worker_loop(self, worker_id: int) -> None:
        from backend.agents.orchestrator import run_task

        while True:
            task_id = await self._queue.get()
            try:
                prompt = await asyncio.to_thread(_load_task_prompt, task_id)
                if prompt is None:
                    # Task row deleted (e.g. cancelled) before we picked it up.
                    continue
                handle = asyncio.ensure_future(run_task(prompt, task_id))
                self._inflight[task_id] = handle
                try:
                    await handle
                except asyncio.CancelledError:
                    if self._stopping:
                        # The pool is shutting down — propagate after cleanup.
                        if not handle.done():
                            handle.cancel()
                        raise
                    # Otherwise the task itself was cancelled (request_cancel).
                    logger.info(f"Task {task_id} cancelled in worker {worker_id}")
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Task {task_id} failed in worker {worker_id}: {e}")
                    try:
                        await asyncio.to_thread(_finalize_failed, task_id, str(e))
                    except Exception:
                        pass
            except asyncio.CancelledError:
                # Worker itself is being shut down.
                raise
            finally:
                self._inflight.pop(task_id, None)
                self._queue.task_done()


_pool: TaskWorkerPool | None = None


def get_pool() -> TaskWorkerPool:
    global _pool
    if _pool is None:
        _pool = TaskWorkerPool()
    return _pool


def reset_pool() -> None:
    """Test hook — drop the singleton so a fresh pool binds to a new event loop."""
    global _pool
    _pool = None
