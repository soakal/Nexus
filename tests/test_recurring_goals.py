"""Tests for recurring-goal cadence + scheduler tick (council w33gixx93).

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine.
Worker-pool enqueue is always patched so tests never dispatch a real Task.
SystemState row is seeded explicitly so autonomy kill-switch tests are deterministic.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401  — registers all table metadata


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

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


def _mock_pool():
    pool = MagicMock()
    pool.enqueue = AsyncMock()
    return pool


def _seed_state(eng, autonomy: bool = True):
    """Seed the SystemState row (id=1) with the desired autonomy flag."""
    from backend.database import SystemState
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            s.add(SystemState(id=1, autonomy_enabled=autonomy))
        else:
            row.autonomy_enabled = autonomy
            s.add(row)
        s.commit()


# ---------------------------------------------------------------------------
# 1. Columns / model: Goal with recurring fields can be created and read back;
#    _goal_to_dict surfaces all four new keys.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_goal_recurring_columns_round_trip(eng):
    from backend.agents import goals
    from backend.database import Goal

    due_time = datetime.utcnow() + timedelta(days=1)
    with Session(eng) as s:
        g = Goal(
            title="Daily standup",
            description="Run daily standup digest.",
            cadence="daily",
            category="operations",
            success_criteria="Digest delivered to phone.",
            next_eval_at=due_time,
            fingerprint="aabbccddeeff0011",
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        goal_id = g.id

    d = await __import__("asyncio").to_thread(goals._db_get_goal, goal_id)
    assert d is not None
    assert d["cadence"] == "daily"
    assert d["category"] == "operations"
    assert d["success_criteria"] == "Digest delivered to phone."
    assert d["next_eval_at"] is not None


# ---------------------------------------------------------------------------
# 2. propose persists cadence, category, success_criteria.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_propose_persists_cadence_fields(eng):
    from backend.agents import goals
    from backend.database import Goal

    # "monitoring" is a valid category in GOAL_CATEGORIES; "reporting" was a
    # free-form string from before the controlled vocabulary was introduced.
    result = await goals.propose(
        "Weekly report",
        "Generate weekly system health report.",
        cadence="weekly",
        category="monitoring",
        success_criteria="Report sent to Obsidian.",
    )

    assert result["status"] == "proposed"
    g = result["goal"]
    assert g["cadence"] == "weekly"
    assert g["category"] == "monitoring"
    assert g["success_criteria"] == "Report sent to Obsidian."
    assert g["next_eval_at"] is None  # not set until approve

    # Verify in DB too.
    with Session(eng) as s:
        row = s.get(Goal, g["id"])
    assert row.cadence == "weekly"
    assert row.category == "monitoring"
    assert row.success_criteria == "Report sent to Obsidian."
    assert row.next_eval_at is None


# ---------------------------------------------------------------------------
# 3. approve on a cadence="weekly" goal sets next_eval_at ~now+7d,
#    status running, task_id set, enqueue awaited once.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_weekly_goal_sets_next_eval_at(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal

    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    result = await goals.propose(
        "Weekly backup",
        "Run full system backup.",
        cadence="weekly",
    )
    goal_id = result["goal"]["id"]

    before_approve = datetime.utcnow()
    r = await goals.approve(goal_id)

    assert r["status"] == "approved"
    assert r["task_id"] is not None
    assert r["goal"]["status"] == "running"
    assert r["goal"]["task_id"] == r["task_id"]

    # next_eval_at should be ~7 days from now (within a 10-second tolerance).
    nea_str = r["goal"]["next_eval_at"]
    assert nea_str is not None
    nea = datetime.fromisoformat(nea_str)
    expected_low = before_approve + timedelta(days=7) - timedelta(seconds=10)
    expected_high = before_approve + timedelta(days=7) + timedelta(seconds=10)
    assert expected_low <= nea <= expected_high

    # enqueue called once with the task id.
    pool.enqueue.assert_awaited_once_with(r["task_id"])


# ---------------------------------------------------------------------------
# 4. approve on a one-shot (cadence=None) goal does NOT set next_eval_at.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approve_oneshot_no_next_eval_at(eng, monkeypatch):
    from backend.agents import goals

    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    result = await goals.propose("One-time task", "Do something once.")
    goal_id = result["goal"]["id"]

    r = await goals.approve(goal_id)
    assert r["status"] == "approved"
    assert r["goal"]["next_eval_at"] is None


# ---------------------------------------------------------------------------
# 5. _db_due_recurring_goals returns only goals that are due.
# ---------------------------------------------------------------------------

def test_db_due_recurring_goals(eng):
    from backend.agents.goals import _db_due_recurring_goals
    from backend.database import Goal

    now = datetime.utcnow()

    with Session(eng) as s:
        # Should be returned: cadence set, next_eval_at in the past, status "completed".
        g_due = Goal(
            title="Due goal",
            description="This is due.",
            cadence="daily",
            next_eval_at=now - timedelta(hours=1),
            status="completed",
            fingerprint="due0000000000001",
        )
        # Should NOT be returned: next_eval_at in the future.
        g_future = Goal(
            title="Future goal",
            description="Not yet due.",
            cadence="weekly",
            next_eval_at=now + timedelta(days=3),
            status="completed",
            fingerprint="fut0000000000002",
        )
        # Should NOT be returned: no cadence (one-shot).
        g_oneshot = Goal(
            title="Oneshot goal",
            description="No recurrence.",
            cadence=None,
            next_eval_at=now - timedelta(hours=2),
            status="completed",
            fingerprint="one0000000000003",
        )
        s.add(g_due)
        s.add(g_future)
        s.add(g_oneshot)
        s.commit()

    due_list = _db_due_recurring_goals(now)
    titles = {d["title"] for d in due_list}
    assert "Due goal" in titles
    assert "Future goal" not in titles
    assert "Oneshot goal" not in titles


# ---------------------------------------------------------------------------
# 6. tick_recurring_goals re-dispatches a due goal: new Task row, enqueue
#    awaited, goal status "running", task_id set, next_eval_at advanced.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_recurring_goals_dispatches_due(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal, Task, SystemState

    _seed_state(eng, autonomy=True)

    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    now = datetime.utcnow()
    past_time = now - timedelta(hours=2)

    with Session(eng) as s:
        g = Goal(
            title="Daily digest",
            description="Send daily digest.",
            cadence="daily",
            next_eval_at=past_time,
            status="completed",
            fingerprint="tick000000000001",
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        goal_id = g.id

    result = await goals.tick_recurring_goals()
    assert result == {"redispatched": 1}

    # enqueue was called once.
    assert pool.enqueue.await_count == 1
    dispatched_task_id = pool.enqueue.call_args[0][0]

    # Task row was created.
    with Session(eng) as s:
        task = s.get(Task, dispatched_task_id)
    assert task is not None
    assert task.prompt == "Goal: Daily digest\n\nSend daily digest."

    # Goal status updated to "running", task_id set, next_eval_at advanced.
    with Session(eng) as s:
        g_after = s.get(Goal, goal_id)
    assert g_after.status == "running"
    assert g_after.task_id == dispatched_task_id
    assert g_after.next_eval_at is not None
    # next_eval_at advanced to ~now+1 day.
    assert g_after.next_eval_at > now


# ---------------------------------------------------------------------------
# 7. Kill switch off → tick returns skipped, no Task created, enqueue NOT awaited.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_recurring_goals_kill_switch_off(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal, Task

    _seed_state(eng, autonomy=False)

    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    now = datetime.utcnow()
    with Session(eng) as s:
        g = Goal(
            title="Should not run",
            description="This must not be dispatched.",
            cadence="daily",
            next_eval_at=now - timedelta(hours=1),
            status="completed",
            fingerprint="kill000000000001",
        )
        s.add(g)
        s.commit()

    result = await goals.tick_recurring_goals()
    assert result.get("skipped") == "autonomy_disabled"

    # No Task created, enqueue never called.
    pool.enqueue.assert_not_awaited()
    with Session(eng) as s:
        tasks = s.exec(select(Task)).all()
    assert len(tasks) == 0


# ---------------------------------------------------------------------------
# 8. Best-effort: if _db_create_task raises for one goal, tick does not raise
#    and still returns a result dict.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_recurring_goals_best_effort(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal, SystemState

    _seed_state(eng, autonomy=True)

    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    now = datetime.utcnow()
    with Session(eng) as s:
        g = Goal(
            title="Failing goal",
            description="Will explode in _db_create_task.",
            cadence="weekly",
            next_eval_at=now - timedelta(minutes=5),
            status="completed",
            fingerprint="best000000000001",
        )
        s.add(g)
        s.commit()

    # Patch _db_create_task to always raise.
    with patch("backend.agents.goals._db_create_task", side_effect=RuntimeError("DB exploded")):
        result = await goals.tick_recurring_goals()

    # Must not raise; returns a dict.
    assert isinstance(result, dict)
    # redispatched is 0 because the only goal failed.
    assert result.get("redispatched", 0) == 0
    # enqueue was never called.
    pool.enqueue.assert_not_awaited()


# ---------------------------------------------------------------------------
# 9. Scheduler registers "goal_recurrence" when goal_recurrence_enabled=True.
#    (Updates test_coverage_boost.py count — verified there; this test is additive.)
# ---------------------------------------------------------------------------

def test_scheduler_goal_recurrence_job_registered():
    from backend.scheduler import setup_scheduler, scheduler
    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:30", "America/New_York")

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "goal_recurrence" in ids


def test_scheduler_goal_recurrence_disabled(monkeypatch):
    """When goal_recurrence_enabled=False, the job is NOT registered."""
    from backend.scheduler import setup_scheduler, scheduler
    from backend.config import get_settings

    s = get_settings()
    original = getattr(s, "goal_recurrence_enabled", True)
    try:
        object.__setattr__(s, "goal_recurrence_enabled", False)
        with patch.object(scheduler, "add_job") as mock_add:
            setup_scheduler("07:30", "America/New_York")
        ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
        assert "goal_recurrence" not in ids
    finally:
        object.__setattr__(s, "goal_recurrence_enabled", original)


# ---------------------------------------------------------------------------
# 10. NO OVERLAP: a recurring goal still 'running' with an UNFINISHED task is
#     NOT re-dispatched (reconcile leaves it running; the due query excludes
#     'running'). Guarantees a recurring goal never overlaps itself.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_does_not_overlap_running_goal(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal, Task

    _seed_state(eng, autonomy=True)
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    past = datetime.utcnow() - timedelta(hours=2)
    with Session(eng) as s:
        t = Task(prompt="long run", status="running")  # task still in flight
        s.add(t)
        s.commit()
        s.refresh(t)
        g = Goal(
            title="Long recurring", description="long", cadence="daily",
            next_eval_at=past, status="running", task_id=t.id,
            fingerprint="overlap000000001",
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        goal_id, old_task_id = g.id, t.id

    result = await goals.tick_recurring_goals()

    assert result.get("redispatched", 0) == 0       # NOT re-dispatched
    pool.enqueue.assert_not_awaited()
    with Session(eng) as s:
        g_after = s.get(Goal, goal_id)
    assert g_after.status == "running"              # untouched
    assert g_after.task_id == old_task_id           # no new task swapped in


# ---------------------------------------------------------------------------
# 11. RECONCILE-FIRST: a recurring goal marked 'running' whose task has actually
#     SUCCEEDED is advanced to 'completed' by the tick's reconcile pass and then
#     re-dispatched (so a finished-but-unreconciled goal still recurs).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tick_reconciles_then_redispatches(eng, monkeypatch):
    from backend.agents import goals
    from backend.database import Goal, Task

    _seed_state(eng, autonomy=True)
    pool = _mock_pool()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool)

    past = datetime.utcnow() - timedelta(hours=2)
    with Session(eng) as s:
        t = Task(prompt="done run", status="success")  # task finished
        s.add(t)
        s.commit()
        s.refresh(t)
        g = Goal(
            title="Finished recurring", description="recur me", cadence="daily",
            next_eval_at=past, status="running", task_id=t.id,
            fingerprint="reconcile0000001",
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        goal_id = g.id

    result = await goals.tick_recurring_goals()

    assert result.get("redispatched", 0) == 1       # reconciled -> completed -> re-run
    assert pool.enqueue.await_count == 1
    with Session(eng) as s:
        g_after = s.get(Goal, goal_id)
    assert g_after.status == "running"              # fresh cycle running
    assert g_after.next_eval_at > datetime.utcnow()  # schedule advanced
