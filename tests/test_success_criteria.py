"""Tests for Goal.success_criteria evaluation in reconcile_running.

Covers:
  1. Goal WITH criteria + task success + haiku returns met=true  → completed
  2. Goal WITH criteria + task success + haiku returns met=false → failed + backoff + rejection_reason
  3. Goal WITHOUT criteria + task success                        → completed, haiku NOT called
  4. Eval best-effort: haiku raises                             → completed (mechanical success)
  5. success_criteria_eval_enabled=False                         → completed, haiku NOT called
  6. _db_get_task_result: returns result_json (or None)

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine.
All haiku calls are patched so no LLM billing occurs.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine

from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — registers all table metadata


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


def _seed_running_goal(eng, *, task_id: int, success_criteria: str | None = None,
                        attempts: int = 0, fingerprint: str = "testfp0000000001") -> int:
    """Insert a running Goal linked to task_id. Returns the Goal id."""
    from backend.database import Goal
    with Session(eng) as s:
        g = Goal(
            title="Free up disk space",
            description="Delete temp files to free up disk.",
            status="running",
            fingerprint=fingerprint,
            task_id=task_id,
            attempts=attempts,
            success_criteria=success_criteria,
        )
        s.add(g)
        s.commit()
        s.refresh(g)
        return g.id


def _seed_task(eng, *, status: str, result_json: str | None = None) -> int:
    """Insert a Task with the given status and result_json. Returns the Task id."""
    from backend.database import Task
    with Session(eng) as s:
        t = Task(prompt="do the thing", status=status, result_json=result_json)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.id


# ---------------------------------------------------------------------------
# 1. Goal WITH criteria + haiku met=true → completed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_criteria_met_goal_completed(eng):
    """When haiku says met=true, the goal transitions to 'completed'."""
    from backend.agents import goals
    from backend.database import Goal

    task_id = _seed_task(eng, status="success", result_json='{"output": "disk freed"}')
    goal_id = _seed_running_goal(
        eng,
        task_id=task_id,
        success_criteria="Disk usage below 80%.",
    )

    haiku_response = '{"met": true, "reason": "disk usage dropped to 70%"}'

    with patch("backend.agents.router.haiku", new_callable=AsyncMock, return_value=haiku_response):
        await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)

    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "completed"
    assert g.attempts == 0  # unchanged — success path never increments attempts


# ---------------------------------------------------------------------------
# 2. Goal WITH criteria + haiku met=false → failed + backoff + rejection_reason
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_criteria_not_met_goal_failed_with_backoff(eng):
    """When haiku says met=false, the goal transitions to 'failed' with backoff + reason."""
    from backend.agents import goals
    from backend.database import Goal

    task_id = _seed_task(eng, status="success", result_json='{"output": "still full"}')
    goal_id = _seed_running_goal(
        eng,
        task_id=task_id,
        success_criteria="Disk usage below 80%.",
        attempts=0,
        fingerprint="testfp0000000002",
    )

    haiku_response = '{"met": false, "reason": "storage still 95%"}'

    with patch("backend.agents.router.haiku", new_callable=AsyncMock, return_value=haiku_response):
        await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)

    with Session(eng) as s:
        g = s.get(Goal, goal_id)

    assert g.status == "failed"
    assert g.attempts == 1
    assert g.backoff_until is not None
    assert g.backoff_until > datetime.utcnow()
    # backoff_until should be ~300 * 2^1 = 600 seconds from now
    expected_low = datetime.utcnow() + timedelta(seconds=590)
    expected_high = datetime.utcnow() + timedelta(seconds=610)
    assert expected_low <= g.backoff_until <= expected_high
    assert g.rejection_reason is not None
    assert "criteria_not_met" in g.rejection_reason
    assert "storage still 95%" in g.rejection_reason


# ---------------------------------------------------------------------------
# 3. Goal WITHOUT criteria + task success → completed, haiku NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_criteria_goal_completed_without_haiku(eng):
    """A goal with no success_criteria is marked completed mechanically; haiku is never called."""
    from backend.agents import goals
    from backend.database import Goal

    task_id = _seed_task(eng, status="success")
    goal_id = _seed_running_goal(
        eng,
        task_id=task_id,
        success_criteria=None,  # explicitly None
        fingerprint="testfp0000000003",
    )

    # goal_outcome_distill_llm defaults True (2026-07-07): mock the completion-side
    # fact-extraction call so it can't reach router.haiku either -- this test is
    # specifically about the success-criteria-eval path never calling haiku.
    haiku_mock = AsyncMock()
    with patch("backend.agents.router.haiku", haiku_mock), \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock):
        await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)

    haiku_mock.assert_not_awaited()
    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "completed"


# ---------------------------------------------------------------------------
# 4. Eval best-effort: haiku raises → goal completed (mechanical success stands)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_criteria_eval_haiku_raises_falls_back_to_completed(eng):
    """If haiku raises (e.g. BudgetExceeded or network error), the goal is still completed."""
    from backend.agents import goals
    from backend.database import Goal
    from backend.safety.governor import BudgetExceeded

    task_id = _seed_task(eng, status="success", result_json='{"output": "done"}')
    goal_id = _seed_running_goal(
        eng,
        task_id=task_id,
        success_criteria="The job must finish under budget.",
        fingerprint="testfp0000000004",
    )

    with patch(
        "backend.agents.router.haiku",
        new_callable=AsyncMock,
        side_effect=BudgetExceeded("daily", 25.0, 25.0),
    ):
        # Must NOT raise — best-effort fallback keeps goal completed.
        await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)

    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "completed"
    assert g.attempts == 0


# ---------------------------------------------------------------------------
# 5. success_criteria_eval_enabled=False → completed, haiku NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_eval_disabled_goal_completed_without_haiku(eng):
    """When success_criteria_eval_enabled=False, criteria are ignored and haiku is not called."""
    from backend.agents import goals
    from backend.config import get_settings
    from backend.database import Goal

    task_id = _seed_task(eng, status="success")
    goal_id = _seed_running_goal(
        eng,
        task_id=task_id,
        success_criteria="This should be ignored.",
        fingerprint="testfp0000000005",
    )

    s = get_settings()
    original = getattr(s, "success_criteria_eval_enabled", True)
    try:
        object.__setattr__(s, "success_criteria_eval_enabled", False)
        haiku_mock = AsyncMock()
        # goal_outcome_distill_llm defaults True (2026-07-07): mock the
        # completion-side fact-extraction call so it can't reach router.haiku.
        with patch("backend.agents.router.haiku", haiku_mock), \
             patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock):
            await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)
    finally:
        object.__setattr__(s, "success_criteria_eval_enabled", original)

    haiku_mock.assert_not_awaited()
    with Session(eng) as s:
        g = s.get(Goal, goal_id)
    assert g.status == "completed"


# ---------------------------------------------------------------------------
# 6. _db_get_task_result returns result_json (or None for missing task)
# ---------------------------------------------------------------------------

def test_db_get_task_result_returns_json(eng):
    """_db_get_task_result returns the stored result_json for a Task."""
    from backend.agents.goals import _db_get_task_result
    from backend.database import Task

    with Session(eng) as s:
        t = Task(prompt="test", status="success", result_json='{"answer": 42}')
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id

    result = _db_get_task_result(task_id)
    assert result == '{"answer": 42}'


def test_db_get_task_result_returns_none_for_missing(eng):
    """_db_get_task_result returns None when the Task row doesn't exist."""
    from backend.agents.goals import _db_get_task_result

    result = _db_get_task_result(99999)
    assert result is None


def test_db_get_task_result_returns_none_when_result_json_null(eng):
    """_db_get_task_result returns None when result_json is NULL."""
    from backend.agents.goals import _db_get_task_result
    from backend.database import Task

    with Session(eng) as s:
        t = Task(prompt="test", status="running", result_json=None)
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id

    result = _db_get_task_result(task_id)
    assert result is None
