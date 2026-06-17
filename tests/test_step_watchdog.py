"""Tests for the live hung-step watchdog (reap_hung_steps).

Safety contract being verified:
- A 'running' step whose task_id IS in pool._inflight is NEVER reaped (ALIVE).
- A 'running' step whose heartbeat is recent (within timeout_s) is NOT reaped (FRESH).
- A 'running' step with stale/NULL heartbeat whose task_id is NOT in _inflight IS reaped.
- Best-effort: a per-step error does not abort the whole sweep.
- heartbeat_at=None is treated the same as a stale timestamp.
- The scheduler job 'step_watchdog' is registered when step_watchdog_enabled=True.
"""
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — register models before create_all


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def eng(monkeypatch):
    e = _make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _seed_task(eng, prompt="watchdog-task", status="running"):
    from backend.database import Task

    with Session(eng) as s:
        t = Task(prompt=prompt, status=status)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


def _seed_step(eng, task_id, status="running", heartbeat_at=None, step_index=1):
    from backend.database import TaskStep

    with Session(eng) as s:
        step = TaskStep(
            task_id=task_id,
            step_index=step_index,
            prompt="do something",
            status=status,
            heartbeat_at=heartbeat_at,
        )
        s.add(step)
        s.commit()
        s.refresh(step)
        return step.id


def _get_step_status(eng, step_id):
    from backend.database import TaskStep

    with Session(eng) as s:
        step = s.get(TaskStep, step_id)
        return step.status if step else None


def _get_step_heartbeat(eng, step_id):
    from backend.database import TaskStep

    with Session(eng) as s:
        step = s.get(TaskStep, step_id)
        return step.heartbeat_at if step else "MISSING"


# ---------------------------------------------------------------------------
# Test 1 — ORPHAN reaped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reap_orphaned_step(eng):
    """A stale running step with no in-flight worker is reset to pending and re-enqueued."""
    from backend.agents.worker_pool import TaskWorkerPool, reset_pool

    reset_pool()
    task_id = _seed_task(eng, status="running")
    stale_hb = datetime.utcnow() - timedelta(seconds=700)
    step_id = _seed_step(eng, task_id, status="running", heartbeat_at=stale_hb)

    pool = TaskWorkerPool(size=0)  # no workers — queue builds up, no draining
    assert task_id not in pool._inflight  # confirm orphaned

    count = await pool.reap_hung_steps(600)

    assert count == 1
    assert _get_step_status(eng, step_id) == "pending"
    assert _get_step_heartbeat(eng, step_id) is None
    # The task must have been enqueued (re-enqueue puts it in the queue via
    # _set_task_pending + _queue.put)
    assert pool._queue.qsize() == 1


# ---------------------------------------------------------------------------
# Test 2 — ALIVE skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_alive_step_skipped(eng):
    """A stale running step whose task_id IS in _inflight is NEVER touched."""
    from backend.agents.worker_pool import TaskWorkerPool, reset_pool

    reset_pool()
    task_id = _seed_task(eng, status="running")
    stale_hb = datetime.utcnow() - timedelta(seconds=700)
    step_id = _seed_step(eng, task_id, status="running", heartbeat_at=stale_hb)

    pool = TaskWorkerPool(size=0)
    # Simulate an active worker holding this task
    pool._inflight[task_id] = object()  # type: ignore[assignment]

    count = await pool.reap_hung_steps(600)

    assert count == 0
    # Step must still be running — watchdog must NOT have touched it
    assert _get_step_status(eng, step_id) == "running"
    assert pool._queue.qsize() == 0


# ---------------------------------------------------------------------------
# Test 3 — FRESH skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_step_skipped(eng):
    """A running step with a recent heartbeat (within the window) is not reaped."""
    from backend.agents.worker_pool import TaskWorkerPool, reset_pool

    reset_pool()
    task_id = _seed_task(eng, status="running")
    # heartbeat very recent — well within the 600-second window
    fresh_hb = datetime.utcnow() - timedelta(seconds=30)
    step_id = _seed_step(eng, task_id, status="running", heartbeat_at=fresh_hb)

    pool = TaskWorkerPool(size=0)
    assert task_id not in pool._inflight  # not in-flight, but heartbeat is fresh

    count = await pool.reap_hung_steps(600)

    assert count == 0
    assert _get_step_status(eng, step_id) == "running"
    assert pool._queue.qsize() == 0


# ---------------------------------------------------------------------------
# Test 4 — heartbeat NULL treated as hung
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_null_heartbeat_treated_as_hung(eng):
    """A running step with heartbeat_at=None is treated as orphaned/stale."""
    from backend.agents.worker_pool import TaskWorkerPool, reset_pool

    reset_pool()
    task_id = _seed_task(eng, status="running")
    step_id = _seed_step(eng, task_id, status="running", heartbeat_at=None)

    pool = TaskWorkerPool(size=0)
    assert task_id not in pool._inflight

    count = await pool.reap_hung_steps(600)

    assert count == 1
    assert _get_step_status(eng, step_id) == "pending"
    assert pool._queue.qsize() == 1


# ---------------------------------------------------------------------------
# Test 5 — best-effort: one bad step does not abort the sweep
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_best_effort_one_failure_does_not_abort(eng):
    """If _reset_step_to_pending raises for one step, the rest are still processed."""
    from backend.agents.worker_pool import TaskWorkerPool, reset_pool

    reset_pool()

    # Two orphaned steps
    task_id_a = _seed_task(eng, status="running")
    task_id_b = _seed_task(eng, status="running")
    stale_hb = datetime.utcnow() - timedelta(seconds=700)
    step_id_a = _seed_step(eng, task_id_a, status="running", heartbeat_at=stale_hb)
    step_id_b = _seed_step(eng, task_id_b, status="running", heartbeat_at=stale_hb, step_index=1)

    pool = TaskWorkerPool(size=0)

    call_count = {"n": 0}
    original_reset = None

    # Patch _reset_step_to_pending: raise on the first call, succeed on subsequent
    from backend.agents import worker_pool as wp_module

    original_reset = wp_module._reset_step_to_pending

    def flaky_reset(sid):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("injected DB error")
        original_reset(sid)

    with patch.object(wp_module, "_reset_step_to_pending", side_effect=flaky_reset):
        count = await pool.reap_hung_steps(600)

    # Should not raise; should have processed the second step even though first failed
    # count is 1 because the first step errored (not counted) but second succeeded
    assert count == 1
    # The second step should have been reset
    # (we can't be sure which step was processed first without controlling order,
    # but at least one was reaped and no exception escaped)
    statuses = {_get_step_status(eng, step_id_a), _get_step_status(eng, step_id_b)}
    assert "pending" in statuses  # at least one was reaped


# ---------------------------------------------------------------------------
# Test 6 — scheduler registers step_watchdog job when enabled
# ---------------------------------------------------------------------------

def test_scheduler_registers_step_watchdog_when_enabled():
    """setup_scheduler adds the 'step_watchdog' job when step_watchdog_enabled=True."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:00", "America/Detroit")

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "step_watchdog" in ids


def test_scheduler_omits_step_watchdog_when_disabled():
    """setup_scheduler does NOT add 'step_watchdog' when step_watchdog_enabled=False."""
    from backend.scheduler import setup_scheduler, scheduler
    from backend.config import Settings

    # Provide a settings instance with the watchdog disabled.
    # setup_scheduler calls `from backend.config import get_settings` locally,
    # so we patch at the source module level.
    disabled_settings = Settings(step_watchdog_enabled=False)
    with patch("backend.config.get_settings", return_value=disabled_settings), \
         patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:00", "America/Detroit")

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "step_watchdog" not in ids


# ---------------------------------------------------------------------------
# Test 7 — _step_watchdog coroutine calls reap_hung_steps
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_watchdog_coroutine_calls_reap():
    """The _step_watchdog scheduler wrapper calls pool.reap_hung_steps with the
    configured timeout and does not raise on success."""
    from backend.scheduler import _step_watchdog

    mock_pool = AsyncMock()
    mock_pool.reap_hung_steps = AsyncMock(return_value=3)

    with patch("backend.agents.worker_pool.get_pool", return_value=mock_pool):
        await _step_watchdog()  # must not raise

    mock_pool.reap_hung_steps.assert_called_once()


@pytest.mark.asyncio
async def test_step_watchdog_coroutine_swallows_exception():
    """The _step_watchdog wrapper swallows exceptions so a watchdog failure
    never propagates to the APScheduler job runner."""
    from backend.scheduler import _step_watchdog

    mock_pool = AsyncMock()
    mock_pool.reap_hung_steps = AsyncMock(side_effect=RuntimeError("pool exploded"))

    with patch("backend.agents.worker_pool.get_pool", return_value=mock_pool):
        await _step_watchdog()  # must not raise
