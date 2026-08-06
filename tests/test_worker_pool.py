import asyncio
import json

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


# --- task-finished notification (worker_pool._notify_task_finished) ---------
#
# NOTE: every test here seeds its Task row AFTER pool.start(), never before —
# pool.start() calls requeue_unfinished(), which re-enqueues ANY existing
# pending/running Task row. Seeding first would double-enqueue (once via
# requeue_unfinished, once via the test's own explicit enqueue()), running
# fake_run_task twice and breaking the assert_awaited_once() assertions below.


@pytest.mark.asyncio
async def test_success_notifies_phone(eng):
    """A standalone task reaching 'success' pages task_completed with the
    last non-empty step output as the summary."""
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            t.status = "success"
            t.result_json = json.dumps(["step one output", "Removed 4 dangling images."])
            t.steps_taken = 2
            s.commit()

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng, prompt="clean up docker images")
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_awaited_once()
    content = notify.call_args.args[0]
    assert notify.call_args.kwargs["kind"] == "task_completed"
    assert f"#{task_id}" in content
    assert "Removed 4 dangling images." in content


@pytest.mark.asyncio
async def test_failure_step_exhausted_notifies(eng):
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            t.status = "failed"
            t.result_json = json.dumps({"error": "step_exhausted", "step_index": 2, "attempts": 5})
            s.commit()

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng)
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_awaited_once()
    content = notify.call_args.args[0]
    assert notify.call_args.kwargs["kind"] == "task_failed"
    assert "step 2" in content
    assert "5 attempts" in content


@pytest.mark.asyncio
async def test_failure_budget_exceeded_notifies(eng):
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            t.status = "failed"
            t.result_json = json.dumps(
                {"error": "budget_exceeded", "scope": "task", "spend": 5.12, "cap": 5.0}
            )
            s.commit()

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng)
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_awaited_once()
    content = notify.call_args.args[0]
    assert notify.call_args.kwargs["kind"] == "task_failed"
    assert "budget exceeded" in content


@pytest.mark.asyncio
async def test_worker_crash_notifies_phone(eng):
    """A worker-level crash goes through _finalize_failed, which still
    counts as a real 'failed' terminal state for the notification."""
    from backend.agents.worker_pool import TaskWorkerPool

    async def fake_run_task(prompt, tid):
        raise RuntimeError("boom")

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng)
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_awaited_once()
    content = notify.call_args.args[0]
    assert notify.call_args.kwargs["kind"] == "task_failed"
    assert "boom" in content


@pytest.mark.asyncio
async def test_stopped_status_does_not_notify(eng):
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            t.status = "stopped"
            s.commit()

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng)
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_left_running_does_not_notify(eng):
    """A task left in 'running' (hard-cancel mid-await never reaches a
    finalizer) must not page — only a real terminal state does."""
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            t.status = "running"
            s.commit()

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng)
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_goal_backed_task_does_not_notify(eng):
    """The most important regression guard — prevents double-paging on every
    Goal-backed run. goals.py's reconcile_running already owns that
    notification lifecycle (obsidian.emit_event + its own notify_phone)."""
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Goal, Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            t.status = "success"
            t.result_json = json.dumps(["done"])
            s.commit()

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng)
        with Session(eng) as s:
            s.add(Goal(title="do the thing", description="do the thing", status="running", task_id=task_id))
            s.commit()
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleted_task_row_does_not_notify(eng):
    """DELETE /api/tasks/{id} calls request_cancel() then deletes the row —
    a cancelled-and-deleted task must produce no notification at all."""
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            s.delete(t)
            s.commit()

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng)
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_failure_never_breaks_the_worker(eng):
    """notify_phone raising must never affect the worker loop — the pool
    stays alive and a second task enqueued afterward still runs to
    completion."""
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            t.status = "success"
            t.result_json = json.dumps(["ok"])
            s.commit()

    notify = AsyncMock(side_effect=RuntimeError("telegram down"))
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng)
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)

        task_id2 = _seed_task(eng, prompt="second task")
        await pool.enqueue(task_id2)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    with Session(eng) as s:
        t2 = s.get(Task, task_id2)
        assert t2.status == "success"


@pytest.mark.asyncio
async def test_prompt_html_is_escaped(eng):
    """notify_phone sends parse_mode=HTML — an unescaped prompt containing
    '<'/'&' would make Telegram reject the whole message as a 400."""
    from backend.agents.worker_pool import TaskWorkerPool
    from backend.database import Task

    async def fake_run_task(prompt, tid):
        with Session(eng) as s:
            t = s.get(Task, tid)
            t.status = "success"
            t.result_json = json.dumps(["done"])
            s.commit()

    notify = AsyncMock(return_value=True)
    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.orchestrator.run_task", new=fake_run_task), \
         patch("backend.events.notify_phone", new=notify):
        await pool.start()
        task_id = _seed_task(eng, prompt="restart <jellyfin> & co")
        await pool.enqueue(task_id)
        await asyncio.wait_for(pool._queue.join(), timeout=5)
        await pool.stop()

    notify.assert_awaited_once()
    content = notify.call_args.args[0]
    assert "<jellyfin>" not in content
    assert "&lt;jellyfin&gt;" in content


def test_task_notify_kinds_are_mutable():
    from backend.safety import governor

    assert {"task_completed", "task_failed"}.isdisjoint(governor._NEVER_MUTABLE_NOTIFY_KINDS)


# --- _summarize_task_result (pure, no engine, no async) ---------------------


def test_summarize_success_list():
    from backend.agents.worker_pool import _summarize_task_result

    rj = json.dumps(["step one output", "Removed 4 dangling images, freed 1.2 GB."])
    assert _summarize_task_result("success", rj) == "Removed 4 dangling images, freed 1.2 GB."


def test_summarize_success_empty_list():
    from backend.agents.worker_pool import _summarize_task_result

    assert _summarize_task_result("success", json.dumps([])) == "completed"


def test_summarize_success_list_of_empty_strings():
    from backend.agents.worker_pool import _summarize_task_result

    assert _summarize_task_result("success", json.dumps(["", "   ", ""])) == "completed"


def test_summarize_budget_exceeded():
    from backend.agents.worker_pool import _summarize_task_result

    rj = json.dumps({"error": "budget_exceeded", "scope": "task", "spend": 5.12, "cap": 5.0})
    out = _summarize_task_result("failed", rj)
    assert "budget exceeded" in out
    assert "$5.12" in out
    assert "$5.00" in out


def test_summarize_step_exhausted():
    from backend.agents.worker_pool import _summarize_task_result

    rj = json.dumps({"error": "step_exhausted", "step_index": 2, "attempts": 5})
    out = _summarize_task_result("failed", rj)
    assert "step 2" in out
    assert "5 attempts" in out


def test_summarize_verify_rejected():
    from backend.agents.worker_pool import _summarize_task_result

    rj = json.dumps({"error": "verify_rejected", "reason": "output did not match spec", "confidence": 0.4})
    out = _summarize_task_result("failed", rj)
    assert out.startswith("verifier rejected:")
    assert "output did not match spec" in out


def test_summarize_generic_error_string():
    from backend.agents.worker_pool import _summarize_task_result

    rj = json.dumps({"error": "max_retries_exceeded"})
    assert _summarize_task_result("failed", rj) == "max_retries_exceeded"


def test_summarize_unknown_error_value():
    from backend.agents.worker_pool import _summarize_task_result

    rj = json.dumps({"error": "some_never_before_seen_failure"})
    assert _summarize_task_result("failed", rj) == "some_never_before_seen_failure"


def test_summarize_none_result_json():
    from backend.agents.worker_pool import _summarize_task_result

    assert _summarize_task_result("failed", None) == "no result recorded"


def test_summarize_unparseable_result_json():
    from backend.agents.worker_pool import _summarize_task_result

    assert _summarize_task_result("failed", "not json{") == "no result recorded"


def test_summarize_long_success_output_truncated():
    from backend.agents.worker_pool import _summarize_task_result

    rj = json.dumps(["x" * 5000])
    out = _summarize_task_result("success", rj)
    assert len(out) <= 300
