"""Tests for the Pulse activity wiring at each choke point: the APScheduler
event listener, the worker pool loop, router/orchestrator trace open/close,
_record_trace_span's ticker pulse, and the broker's terminal-outcome pulse.

Each test confirms the wiring calls into backend/activity.py correctly AND
that the underlying behavior (dispatch, retry, broker decision) is
byte-identical -- these are pure observability additions.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend import activity


@pytest.fixture(autouse=True)
def _reset_activity():
    activity.reset_registry()
    yield
    activity.reset_registry()


# ---------------------------------------------------------------------------
# Scheduler event listener
# ---------------------------------------------------------------------------

def _capture_listener(monkeypatch):
    """Force-register a fresh copy of the activity listener and return the
    captured callback, bypassing the module's real once-only guard (the
    real `scheduler` singleton may already have a listener registered by an
    earlier test file in the same pytest process)."""
    import backend.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_activity_listener_registered", False)
    captured = {}

    def fake_add_listener(callback, mask):
        captured["callback"] = callback

    with patch.object(sched_mod.scheduler, "add_listener", side_effect=fake_add_listener):
        sched_mod._register_activity_listener()
    assert "callback" in captured
    return captured["callback"]


def test_scheduler_listener_registration_is_idempotent(monkeypatch):
    """setup_scheduler() runs once per test file against the SAME module-level
    scheduler across a full pytest run -- the guard must prevent duplicate
    registrations, or listener count (and pulse volume) would grow unbounded."""
    import backend.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "_activity_listener_registered", False)
    calls = []
    with patch.object(sched_mod.scheduler, "add_listener", side_effect=lambda cb, mask: calls.append(cb)):
        sched_mod._register_activity_listener()
        sched_mod._register_activity_listener()
        sched_mod._register_activity_listener()
    assert len(calls) == 1


def test_job_submitted_begins_running_entry(monkeypatch):
    from apscheduler.events import EVENT_JOB_SUBMITTED
    callback = _capture_listener(monkeypatch)

    event = SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="homelab_watch")
    callback(event)

    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["job:homelab_watch"]
    assert e["status"] == "running"
    assert e["actor_type"] == "job"


def test_job_executed_ends_ok_and_pulses_ticker(monkeypatch):
    from apscheduler.events import EVENT_JOB_SUBMITTED, EVENT_JOB_EXECUTED
    callback = _capture_listener(monkeypatch)

    callback(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="homelab_watch"))
    callback(SimpleNamespace(code=EVENT_JOB_EXECUTED, job_id="homelab_watch"))

    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["job:homelab_watch"]
    assert e["status"] == "ok"
    events = activity.snapshot()["events"]
    assert any(ev["actor_id"] == "job:homelab_watch" for ev in events)


def test_job_error_ends_error_with_exception_message(monkeypatch):
    from apscheduler.events import EVENT_JOB_SUBMITTED, EVENT_JOB_ERROR
    callback = _capture_listener(monkeypatch)

    callback(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="db_backup"))
    callback(SimpleNamespace(code=EVENT_JOB_ERROR, job_id="db_backup", exception=RuntimeError("disk full")))

    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["job:db_backup"]
    assert e["status"] == "error"
    assert "disk full" in e["last_error"]
    events = activity.snapshot()["events"]
    assert any(ev["actor_id"] == "job:db_backup" and "disk full" in ev["summary"] for ev in events)


def test_job_missed_pulses_without_creating_board_entry(monkeypatch):
    from apscheduler.events import EVENT_JOB_MISSED
    callback = _capture_listener(monkeypatch)

    callback(SimpleNamespace(code=EVENT_JOB_MISSED, job_id="record_speedtest"))

    assert activity.snapshot()["entries"] == []
    events = activity.snapshot()["events"]
    assert any(ev["actor_id"] == "job:record_speedtest" and ev["event"] == "job_missed" for ev in events)


def test_quiet_job_executed_updates_board_but_not_ticker(monkeypatch):
    """state_refresh_30s etc. are in _TICKER_QUIET_JOBS -- board status must
    still update, but no ticker line, or a 30s job would dominate the ring."""
    from apscheduler.events import EVENT_JOB_SUBMITTED, EVENT_JOB_EXECUTED
    import backend.scheduler as sched_mod
    callback = _capture_listener(monkeypatch)

    assert "state_refresh_30s" in sched_mod._TICKER_QUIET_JOBS

    callback(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="state_refresh_30s"))
    callback(SimpleNamespace(code=EVENT_JOB_EXECUTED, job_id="state_refresh_30s"))

    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["job:state_refresh_30s"]
    assert e["status"] == "ok"  # board still updates
    events = activity.snapshot()["events"]
    assert not any(ev["actor_id"] == "job:state_refresh_30s" for ev in events)  # no ticker noise


def test_listener_error_does_not_propagate(monkeypatch):
    """A broken activity call inside the listener must not crash APScheduler's
    dispatch -- same best-effort contract as everywhere else in this module."""
    callback = _capture_listener(monkeypatch)
    with patch.object(activity, "begin", side_effect=RuntimeError("boom")):
        from apscheduler.events import EVENT_JOB_SUBMITTED
        callback(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="x"))  # must not raise


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_worker_loop_marks_busy_then_idle_around_run_task():
    from backend.agents.worker_pool import TaskWorkerPool

    pool = TaskWorkerPool(size=1)

    with patch("backend.agents.worker_pool._load_task_prompt", return_value="do the thing"), \
         patch("backend.agents.orchestrator.run_task", new_callable=AsyncMock) as mock_run, \
         patch("backend.agents.worker_pool._notify_task_finished", new_callable=AsyncMock):

        seen_status_during_run = {}

        async def _slow_run_task(*a, **kw):
            e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}.get("worker:0")
            seen_status_during_run["status"] = e["status"] if e else None
            seen_status_during_run["detail"] = e["detail"] if e else None
            seen_status_during_run["label"] = e["label"] if e else None
            return SimpleNamespace(success=True)

        mock_run.side_effect = _slow_run_task

        await pool._queue.put(1)
        # Run the loop and cancel it right after the queue drains -- this
        # exercises one real iteration of _worker_loop's body.
        import asyncio
        task = asyncio.create_task(pool._worker_loop(0))
        await pool._queue.join()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert seen_status_during_run["status"] == "running"
    assert seen_status_during_run["detail"] == {"task_id": 1}
    assert seen_status_during_run["label"] == "do the thing"
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["worker:0"]
    assert e["status"] == "ok"
    assert e["started_at"] is None


@pytest.mark.asyncio
async def test_worker_loop_deleted_task_never_begins_or_sets_detail():
    """A task row deleted before pickup (prompt is None) must not create a
    worker entry at all -- begin()/update_detail() are both skipped on that
    early-continue path."""
    from backend.agents.worker_pool import TaskWorkerPool
    import asyncio

    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.worker_pool._load_task_prompt", return_value=None):
        await pool._queue.put(1)
        task = asyncio.create_task(pool._worker_loop(0))
        await pool._queue.join()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert "worker:0" not in {e["actor_id"] for e in activity.snapshot()["entries"]}


@pytest.mark.asyncio
async def test_worker_loop_poisoned_update_detail_does_not_block_task():
    """Constraint test: a poisoned activity.update_detail must not prevent
    the worker from actually running the task, AND must not leave the
    worker's Pulse entry stuck "running" forever. begin() and update_detail()
    are in separate try/excepts specifically so _worker_began only depends on
    begin() succeeding -- a shared try/except (the original, self-caught bug
    in this batch) would skip `_worker_began = True` whenever update_detail
    raised, and since the loop's `finally` only calls activity.end() when
    _worker_began is true, the entry would never transition out of "running".
    `mock_run.assert_awaited_once()` alone does NOT catch that bug (the task
    still runs either way) -- the status assertion below is the one that
    actually discriminates the fix from the original ordering."""
    from backend.agents.worker_pool import TaskWorkerPool
    import asyncio

    pool = TaskWorkerPool(size=1)
    with patch("backend.agents.worker_pool._load_task_prompt", return_value="do the thing"), \
         patch("backend.agents.orchestrator.run_task", new_callable=AsyncMock) as mock_run, \
         patch("backend.agents.worker_pool._notify_task_finished", new_callable=AsyncMock), \
         patch.object(activity, "update_detail", side_effect=RuntimeError("boom")):

        mock_run.return_value = SimpleNamespace(success=True)
        await pool._queue.put(1)
        task = asyncio.create_task(pool._worker_loop(0))
        await pool._queue.join()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    mock_run.assert_awaited_once()
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}["worker:0"]
    assert e["status"] == "ok", f"worker entry stuck as {e['status']!r} -- end() never ran"
    assert e["started_at"] is None


# ---------------------------------------------------------------------------
# router.open_trace / close_trace
# ---------------------------------------------------------------------------

def test_open_trace_begins_trace_entry(monkeypatch):
    import backend.database as db
    from sqlmodel import SQLModel, create_engine
    from sqlmodel.pool import StaticPool

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    from backend.agents.router import open_trace, close_trace

    trace_id = open_trace("chat", "conv:1")
    assert trace_id is not None
    actor_id = f"trace:chat:{trace_id}"
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}[actor_id]
    assert e["status"] == "running"

    close_trace(trace_id, "ok")
    # Removed on close (per-instance, one-shot -- see open_trace's comment on
    # why trace_id is part of the actor id), not left behind as "ok". A
    # left-behind entry per completed trace would grow the registry by one
    # row forever, for every chat/briefing/proposer/voice call ever made.
    assert actor_id not in {e["actor_id"] for e in activity.snapshot()["entries"]}


def test_open_trace_two_concurrent_same_kind_traces_dont_collide(monkeypatch):
    """Regression: two overlapping traces of the SAME kind (e.g. a web chat
    turn and a Telegram chat turn running at once) used to share one board
    entry and clobber each other's started_at/status. Each is now keyed by
    its own trace_id."""
    import backend.database as db
    from sqlmodel import SQLModel, create_engine
    from sqlmodel.pool import StaticPool

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    from backend.agents.router import open_trace, close_trace

    trace_id_a = open_trace("chat", "conv:1")
    trace_id_b = open_trace("chat", "conv:2")
    assert trace_id_a != trace_id_b

    entries = {e["actor_id"]: e for e in activity.snapshot()["entries"]}
    assert entries[f"trace:chat:{trace_id_a}"]["status"] == "running"
    assert entries[f"trace:chat:{trace_id_b}"]["status"] == "running"

    # Finishing A must not affect B's still-running entry.
    close_trace(trace_id_a, "ok")
    entries = {e["actor_id"]: e for e in activity.snapshot()["entries"]}
    assert f"trace:chat:{trace_id_a}" not in entries
    assert entries[f"trace:chat:{trace_id_b}"]["status"] == "running"


def test_close_trace_activity_cleanup_independent_of_db_read_failure(monkeypatch):
    """The activity cleanup must not be gated on the DB session succeeding --
    coupling it to a DB read would leave a phantom "running" Pulse entry for
    up to sweep_stale's 24h backstop on a transient DB failure that has
    nothing to do with activity.py."""
    import backend.database as db
    from sqlmodel import SQLModel, create_engine
    from sqlmodel.pool import StaticPool

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    from backend.agents.router import open_trace, close_trace

    trace_id = open_trace("chat", "conv:1")
    actor_id = f"trace:chat:{trace_id}"
    assert actor_id in {e["actor_id"] for e in activity.snapshot()["entries"]}

    with patch("sqlmodel.Session", side_effect=RuntimeError("db down")):
        close_trace(trace_id, "ok")  # must not raise

    # Removed despite the DB session failing entirely.
    assert actor_id not in {e["actor_id"] for e in activity.snapshot()["entries"]}


def test_close_trace_noop_actor_id_when_trace_id_none():
    from backend.agents.router import close_trace
    close_trace(None, "ok")  # must not raise, no entries touched
    assert activity.snapshot()["entries"] == []


# ---------------------------------------------------------------------------
# orchestrator._open_trace / _close_trace / step progress
# ---------------------------------------------------------------------------

def test_orchestrator_open_trace_begins_task_entry_only(monkeypatch):
    """No separate trace:orchestrator board entry -- task:{id} is already a
    unique per-run identity, and a second shared key here would race across
    NEXUS_TASK_WORKERS' concurrent durable runs (the exact bug a shared
    trace:{kind} key had before this fix -- see router.py's open_trace)."""
    import backend.database as db
    from sqlmodel import SQLModel, create_engine
    from sqlmodel.pool import StaticPool

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    from backend.agents.orchestrator import _open_trace, _close_trace

    trace_id = _open_trace(42, "do the thing")
    entries = {e["actor_id"]: e for e in activity.snapshot()["entries"]}
    assert entries["task:42"]["status"] == "running"
    assert "trace:orchestrator" not in entries

    # _close_trace itself never touched activity (task:42 removal happens at
    # the run_task() call site's finally block, tested separately below via
    # the real durable path) -- confirm it stays untouched by _close_trace
    # alone, not silently removed as a side effect of closing the DB row.
    _close_trace(trace_id, "ok")
    entries = {e["actor_id"]: e for e in activity.snapshot()["entries"]}
    assert entries["task:42"]["status"] == "running"


@pytest.mark.asyncio
async def test_run_task_step_progress_and_task_entry_removed_on_finish(monkeypatch):
    """Real integration test through orchestrator.run_task's durable path:
    task:{id} shows live step progress while running, then is actually
    removed (not just marked idle) once the task finishes -- exercising the
    real call sites in orchestrator.py, not activity.py directly."""
    import backend.database as db
    from sqlmodel import SQLModel, Session, create_engine, select
    from sqlmodel.pool import StaticPool
    from backend.database import Task, TaskStep

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    with Session(eng) as s:
        t = Task(prompt="do the thing", status="pending")
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id
        from backend.agents.orchestrator import _idem_key
        s.add(TaskStep(
            task_id=task_id, step_index=1, prompt="step one", description="the first step",
            status="pending", idempotency_key=_idem_key(task_id, 1, "step one"),
        ))
        s.commit()

    seen_detail = {}

    async def _fake_exec(*a, **kw):
        e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}.get(f"task:{task_id}")
        seen_detail["detail"] = e["detail"] if e else None
        return "done"

    with patch("backend.agents.router.run_with_tools", new=AsyncMock(side_effect=_fake_exec)):
        from backend.agents.orchestrator import run_task
        result = await run_task("do the thing", task_id)

    assert result.success is True
    assert seen_detail["detail"] == {"step_index": 1, "total_steps": 1, "description": "the first step"}
    # Removed, not left "ok" -- a per-task entry left behind after every
    # completed task would grow the registry by one row forever.
    assert f"task:{task_id}" not in {e["actor_id"] for e in activity.snapshot()["entries"]}


# ---------------------------------------------------------------------------
# _record_trace_span pulses the ticker, with or without a trace
# ---------------------------------------------------------------------------

def test_record_trace_span_pulses_even_without_trace_id():
    """B3.3 (Pulse spec) -- untraced calls (mail_drafts, facts, ...) still
    show up in the ticker; the DB write is what's skipped, not the pulse."""
    from backend.agents.router import _record_trace_span
    from datetime import datetime, timedelta

    started = datetime.utcnow() - timedelta(milliseconds=50)
    _record_trace_span("llm_call", "chat_classify (claude-haiku-4-5)", started, trace_id=None)

    events = activity.snapshot()["events"]
    assert len(events) == 1
    assert events[0]["actor_id"] == "llm_call"
    assert "chat_classify" in events[0]["summary"]
    assert "ms" in events[0]["summary"]


def test_record_trace_span_pulse_includes_cost(monkeypatch):
    from backend.agents.router import _record_trace_span
    from datetime import datetime

    fake_resp = MagicMock()
    fake_resp.usage.input_tokens = 1000
    fake_resp.usage.output_tokens = 500
    fake_resp.usage.cache_creation_input_tokens = 0
    fake_resp.usage.cache_read_input_tokens = 0

    _record_trace_span(
        "llm_call", "test_call", datetime.utcnow(), resp=fake_resp,
        model="claude-sonnet-4-6", trace_id=None,
    )
    events = activity.snapshot()["events"]
    assert "$" in events[0]["summary"]


def test_record_trace_span_activity_failure_does_not_break_db_write(monkeypatch):
    """Constraint test: a pulse failure inside _record_trace_span must not
    prevent the TraceSpan DB write that follows it."""
    import backend.database as db
    from sqlmodel import SQLModel, create_engine, Session, select
    from sqlmodel.pool import StaticPool

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    from backend.agents.router import _record_trace_span
    from backend.database import AgentTrace, TraceSpan
    from datetime import datetime

    with Session(eng) as s:
        t = AgentTrace(kind="chat", label="conv:1", status="running")
        s.add(t)
        s.commit()
        s.refresh(t)
        trace_id = t.id

    with patch.object(activity, "pulse", side_effect=RuntimeError("boom")):
        _record_trace_span("llm_call", "chat_classify", datetime.utcnow(), trace_id=trace_id)

    with Session(eng) as s:
        spans = s.exec(select(TraceSpan).where(TraceSpan.trace_id == trace_id)).all()
        assert len(spans) == 1


def test_record_trace_span_updates_trace_detail_with_last_span(monkeypatch):
    """B2 (Pulse detail spec): a completed span updates the trace's Pulse
    entry with the span it just finished, keyed off _open_trace_kinds (only
    populated by router.open_trace -- orchestrator trace_ids are NOT in this
    map, so this must never touch the task progress-bar detail)."""
    import backend.database as db
    from sqlmodel import SQLModel, create_engine
    from sqlmodel.pool import StaticPool
    from datetime import datetime, timedelta

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    from backend.agents.router import open_trace, close_trace, _record_trace_span

    trace_id = open_trace("chat", "conv:1")
    started = datetime.utcnow() - timedelta(milliseconds=42)
    _record_trace_span("llm_call", "chat_classify (claude-haiku-4-5)", started, trace_id=trace_id)

    actor_id = f"trace:chat:{trace_id}"
    e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}[actor_id]
    assert e["detail"]["last_span"] == "chat_classify (claude-haiku-4-5)"
    assert e["detail"]["span_type"] == "llm_call"
    assert e["detail"]["duration_ms"] is not None

    close_trace(trace_id, "ok")
    assert actor_id not in {e["actor_id"] for e in activity.snapshot()["entries"]}


def test_record_trace_span_detail_name_truncated_to_200_chars():
    from backend.agents.router import _record_trace_span, _open_trace_kinds
    from datetime import datetime

    fake_trace_id = 999001
    actor_id = f"trace:chat:{fake_trace_id}"
    _open_trace_kinds[fake_trace_id] = "chat"
    activity.begin(actor_id, "trace", "conv:test")
    try:
        long_name = "x" * 500
        _record_trace_span("llm_call", long_name, datetime.utcnow(), trace_id=fake_trace_id)
        e = {e["actor_id"]: e for e in activity.snapshot()["entries"]}[actor_id]
        assert len(e["detail"]["last_span"]) == 200
    finally:
        _open_trace_kinds.pop(fake_trace_id, None)


def test_record_trace_span_detail_noop_for_unknown_trace_id():
    """A trace_id not in _open_trace_kinds (e.g. an orchestrator trace, which
    only router.open_trace populates) must not create a stray entry or raise."""
    from backend.agents.router import _record_trace_span
    from datetime import datetime

    before = {e["actor_id"] for e in activity.snapshot()["entries"]}
    _record_trace_span("llm_call", "some_call", datetime.utcnow(), trace_id=999999)
    after = {e["actor_id"] for e in activity.snapshot()["entries"]}
    assert after == before


def test_record_trace_span_detail_failure_does_not_break_pulse_or_db(monkeypatch):
    """Constraint test: a poisoned update_detail must not suppress the ticker
    pulse or the TraceSpan DB write -- its own independent try/except."""
    import backend.database as db
    from sqlmodel import SQLModel, create_engine, Session, select
    from sqlmodel.pool import StaticPool
    from datetime import datetime

    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    monkeypatch.setattr(db, "engine", eng)

    from backend.agents.router import open_trace, _record_trace_span
    from backend.database import TraceSpan

    trace_id = open_trace("chat", "conv:1")

    with patch.object(activity, "update_detail", side_effect=RuntimeError("boom")):
        _record_trace_span("llm_call", "chat_classify", datetime.utcnow(), trace_id=trace_id)

    events = activity.snapshot()["events"]
    assert len(events) >= 1  # ticker pulse still fired

    with Session(eng) as s:
        spans = s.exec(select(TraceSpan).where(TraceSpan.trace_id == trace_id)).all()
        assert len(spans) == 1


# ---------------------------------------------------------------------------
# Broker terminal-outcome pulse
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broker_publish_action_pulses_ticker():
    from backend.safety.broker import _publish_action

    with patch("backend.events.publish", new_callable=AsyncMock):
        await _publish_action(
            SimpleNamespace(value="agent"), "ha_service", "light.office",
            SimpleNamespace(value="allowed"), SimpleNamespace(value="low"),
            SimpleNamespace(value="reversible"), 99,
        )

    events = activity.snapshot()["events"]
    assert len(events) == 1
    assert events[0]["actor_id"] == "broker"
    assert "ha_service" in events[0]["summary"]
    assert "allowed" in events[0]["summary"]
    assert "light.office" in events[0]["summary"]


@pytest.mark.asyncio
async def test_broker_publish_action_pulse_failure_does_not_break_events_publish():
    from backend.safety.broker import _publish_action

    publish_mock = AsyncMock()
    with patch("backend.events.publish", publish_mock), \
         patch.object(activity, "pulse", side_effect=RuntimeError("boom")):
        await _publish_action(
            SimpleNamespace(value="agent"), "ha_service", "light.office",
            SimpleNamespace(value="allowed"), SimpleNamespace(value="low"),
            SimpleNamespace(value="reversible"), 99,
        )
    publish_mock.assert_awaited_once()
