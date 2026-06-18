"""Tests for the Opus learning loop (Spec items 1-5).

Covers:
1. TaskOutcome row written on successful durable task (verdict="success").
2. Confident failure verdict (verdict="failure", confidence>=0.7) flips a
   fully-executed task to status "failed" with error "verify_rejected", done
   steps preserved.
3. Low-confidence failure (confidence<0.7) or "uncertain" verdict still
   finalizes "success" (conservative gate).
4. _load_learning_context returns "" when no failures exist, and includes a
   failed TaskOutcome's reason when one exists.
5. _opus_plan includes the learning block in its prompt when learning is
   non-empty.
"""
import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

# Ensure all tables (incl. TaskOutcome) are registered on SQLModel.metadata
# before create_all runs.
import backend.database  # noqa: F401,E402


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_task(eng, prompt="task", status="pending"):
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
        step = TaskStep(
            task_id=task_id,
            step_index=index,
            prompt=prompt,
            description=f"step {index}",
            status=status,
            output_json=json.dumps(output) if output is not None else None,
            idempotency_key=_idem_key(task_id, index, prompt),
        )
        s.add(step)
        s.commit()


def _seed_state(eng, autonomy=True, daily=1000.0, per_task=1000.0):
    from backend.database import SystemState

    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        row.daily_budget_usd = daily
        row.per_task_budget_usd = per_task
        s.commit()


def _success_verify_outcome():
    """A canned _opus_verify return value for verdict=success."""
    return {
        "verdict": "success",
        "confidence": 0.95,
        "reason": "task accomplished",
        "grounded": False,
        "evidence": None,
    }


def _failure_verify_outcome(confidence=0.85):
    """A canned _opus_verify return value for verdict=failure."""
    return {
        "verdict": "failure",
        "confidence": confidence,
        "reason": "output did not match task requirements",
        "grounded": False,
        "evidence": None,
    }


def _uncertain_verify_outcome():
    return {
        "verdict": "uncertain",
        "confidence": 0.3,
        "reason": "cannot determine",
        "grounded": False,
        "evidence": None,
    }


# ---------------------------------------------------------------------------
# Test 1: TaskOutcome row written on successful durable task, verdict="success"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_task_outcome_written_on_success(eng):
    """After all steps complete, a TaskOutcome row is written with verdict=success
    and the Task is finalized 'success'."""
    from backend.database import Task, TaskOutcome

    _seed_state(eng)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "do something", status="pending")

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.agents.orchestrator._opus_verify", new_callable=AsyncMock) as mock_verify:

        mock_exec.return_value = "Step output: done"
        mock_verify.return_value = _success_verify_outcome()

        from backend.agents.orchestrator import run_task
        result = await run_task("task", task_id)

    assert result.success is True

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "success"

        outcomes = s.exec(
            select(TaskOutcome).where(TaskOutcome.task_id == task_id)
        ).all()
        assert len(outcomes) == 1
        assert outcomes[0].verdict == "success"
        assert outcomes[0].model == "opus"
        assert outcomes[0].task_id == task_id

    # _opus_verify must have been called with the task prompt and plan.
    mock_verify.assert_awaited_once()
    call_kwargs = mock_verify.call_args
    assert call_kwargs[0][0] == "task"  # task_prompt positional arg


# ---------------------------------------------------------------------------
# Test 2: Confident failure (>=0.7) flips a fully-executed task to "failed"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confident_failure_verdict_rejects_task(eng):
    """verdict=failure + confidence>=0.7 finalizes the task as 'failed' with
    error='verify_rejected'. Done steps are preserved in the DB."""
    from backend.database import Task, TaskOutcome, TaskStep

    _seed_state(eng)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "do something", status="pending")

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.agents.orchestrator._opus_verify", new_callable=AsyncMock) as mock_verify:

        mock_exec.return_value = "Step output: done"
        mock_verify.return_value = _failure_verify_outcome(confidence=0.85)

        from backend.agents.orchestrator import run_task
        result = await run_task("task", task_id)

    assert result.success is False
    assert result.reason == "verify_rejected"

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "failed"
        payload = json.loads(t.result_json)
        assert payload["error"] == "verify_rejected"
        assert payload["confidence"] == 0.85

        # Done step is preserved.
        steps = s.exec(
            select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.status == "done")
        ).all()
        assert len(steps) == 1

        # TaskOutcome row is still written with verdict=failure.
        outcomes = s.exec(
            select(TaskOutcome).where(TaskOutcome.task_id == task_id)
        ).all()
        assert len(outcomes) == 1
        assert outcomes[0].verdict == "failure"


# ---------------------------------------------------------------------------
# Test 3a: Low-confidence failure still finalizes "success"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_low_confidence_failure_still_succeeds(eng):
    """verdict=failure but confidence<0.7 -> conservative gate passes, task
    finalizes 'success'."""
    from backend.database import Task

    _seed_state(eng)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "do something", status="pending")

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.agents.orchestrator._opus_verify", new_callable=AsyncMock) as mock_verify:

        mock_exec.return_value = "Step output: done"
        mock_verify.return_value = _failure_verify_outcome(confidence=0.5)

        from backend.agents.orchestrator import run_task
        result = await run_task("task", task_id)

    assert result.success is True

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "success"


# ---------------------------------------------------------------------------
# Test 3b: "uncertain" verdict still finalizes "success"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uncertain_verdict_still_succeeds(eng):
    """verdict=uncertain -> conservative gate passes, task finalizes 'success'."""
    from backend.database import Task

    _seed_state(eng)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "do something", status="pending")

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.agents.orchestrator._opus_verify", new_callable=AsyncMock) as mock_verify:

        mock_exec.return_value = "Step output: done"
        mock_verify.return_value = _uncertain_verify_outcome()

        from backend.agents.orchestrator import run_task
        result = await run_task("task", task_id)

    assert result.success is True

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "success"


# ---------------------------------------------------------------------------
# Test 3c: "partial" verdict also finalizes "success" (not rejection)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_partial_verdict_still_succeeds(eng):
    """verdict=partial -> conservative gate passes, task finalizes 'success'."""
    from backend.database import Task

    _seed_state(eng)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "do something", status="pending")

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.agents.orchestrator._opus_verify", new_callable=AsyncMock) as mock_verify:

        mock_exec.return_value = "Step output: done"
        mock_verify.return_value = {
            "verdict": "partial",
            "confidence": 0.8,
            "reason": "only partially done",
            "grounded": False,
            "evidence": None,
        }

        from backend.agents.orchestrator import run_task
        result = await run_task("task", task_id)

    assert result.success is True


# ---------------------------------------------------------------------------
# Test 4a: _load_learning_context returns "" when no failures
# ---------------------------------------------------------------------------

def test_load_learning_context_empty_when_no_failures(eng):
    """No failed TaskOutcome or AgentRun rows -> returns empty string."""
    from backend.agents.orchestrator import _load_learning_context

    result = _load_learning_context()
    assert result == ""


# ---------------------------------------------------------------------------
# Test 4b: _load_learning_context includes a failed TaskOutcome's reason
# ---------------------------------------------------------------------------

def test_load_learning_context_includes_failed_outcome(eng):
    """A failed TaskOutcome's reason appears in the learning context."""
    from backend.agents.orchestrator import _load_learning_context
    from backend.database import TaskOutcome

    with Session(eng) as s:
        s.add(TaskOutcome(
            task_id=1,
            verdict="failure",
            confidence=0.9,
            reason="the API returned 404 for that endpoint",
            grounded=False,
        ))
        s.commit()

    result = _load_learning_context()
    assert result != ""
    assert "404" in result or "API" in result
    assert "[failed]" in result


# ---------------------------------------------------------------------------
# Test 4c: _load_learning_context includes failed AgentRun snippets
# ---------------------------------------------------------------------------

def test_load_learning_context_includes_failed_agent_run(eng):
    """A failed AgentRun row (success=False) appears in the learning context."""
    from backend.agents.orchestrator import _load_learning_context
    from backend.database import AgentRun

    with Session(eng) as s:
        s.add(AgentRun(
            task_id=5,
            agent_type="orchestrator",
            model="sonnet",
            prompt_snippet="fetch the latest unraid logs",
            output_snippet="I cannot complete this task",
            success=False,
            duration_ms=500,
        ))
        s.commit()

    result = _load_learning_context()
    assert result != ""
    assert "[failed]" in result


# ---------------------------------------------------------------------------
# Test 4d: _load_learning_context returns "" on error (never raises)
# ---------------------------------------------------------------------------

def test_load_learning_context_never_raises(monkeypatch):
    """Even if the DB is broken, _load_learning_context returns "" silently."""
    from backend.agents.orchestrator import _load_learning_context

    # Point at a non-existent database; the function should swallow the error.
    from sqlmodel import create_engine
    from sqlmodel.pool import StaticPool
    bad_eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Do NOT create tables — any query will fail with "no such table".
    monkeypatch.setattr("backend.database.engine", bad_eng)

    result = _load_learning_context()
    assert result == ""


# ---------------------------------------------------------------------------
# Test 5: _opus_plan includes learning block when non-empty
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_opus_plan_injects_learning_block():
    """When learning is non-empty, _opus_plan includes the PRIOR ATTEMPTS block
    in the prompt sent to opus."""
    captured = {}

    async def fake_opus(model, prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return '{"steps": [{"index": 1, "description": "step", "prompt": "do it"}]}'

    learning_text = "- [failed] the API returned 404\n- [failed] vault search timed out"

    with patch("backend.agents.router.run_model", new=fake_opus):
        from backend.agents.orchestrator import _opus_plan
        plan = await _opus_plan("some task", learning=learning_text)

    assert plan is not None
    assert len(plan.steps) == 1
    prompt = captured["prompt"]
    assert "PRIOR ATTEMPTS THAT FAILED" in prompt
    assert "avoid repeating these mistakes" in prompt
    assert "404" in prompt
    assert "vault search timed out" in prompt


@pytest.mark.asyncio
async def test_opus_plan_no_learning_block_when_empty():
    """When learning is "" (default), _opus_plan does NOT include the learning
    block — the prompt stays clean for first-run tasks."""
    captured = {}

    async def fake_opus(model, prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return '{"steps": [{"index": 1, "description": "step", "prompt": "do it"}]}'

    with patch("backend.agents.router.run_model", new=fake_opus):
        from backend.agents.orchestrator import _opus_plan
        await _opus_plan("some task")

    assert "PRIOR ATTEMPTS" not in captured["prompt"]


# ---------------------------------------------------------------------------
# Test: _opus_verify parse failure returns safe default (never destroys a good task)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.real_opus_verify
async def test_opus_verify_parse_failure_returns_safe_default(eng):
    """If run_with_tools returns garbage JSON, _opus_verify returns
    verdict='uncertain' and does NOT raise.

    Marked real_opus_verify so the autouse auto_mock_opus_verify fixture is
    skipped — we need the REAL _opus_verify implementation here.
    """
    from backend.agents.orchestrator import Plan, Step, _opus_verify

    plan = Plan(task_prompt="test")
    plan.steps.append(Step(index=1, prompt="p", description="d"))

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_rwt:
        mock_rwt.return_value = "Sorry I cannot provide a JSON response here."
        outcome = await _opus_verify("test task", ["output"], plan)

    assert outcome["verdict"] == "uncertain"
    assert outcome["confidence"] == 0.0
    assert outcome["reason"] == "verify_unparseable"
    assert outcome["grounded"] is False
    assert outcome["evidence"] is None


# ---------------------------------------------------------------------------
# Test: _opus_verify propagates BudgetExceeded (not swallowed)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.real_opus_verify
async def test_opus_verify_propagates_budget_exceeded(eng):
    """BudgetExceeded raised by run_with_tools inside _opus_verify is NOT caught
    by the safe-default handler — it propagates so run_task can finalize
    failed/budget_exceeded.

    Marked real_opus_verify so the autouse auto_mock_opus_verify fixture is
    skipped — we need the REAL _opus_verify implementation here.
    """
    from backend.agents.orchestrator import Plan, Step, _opus_verify
    from backend.safety.governor import BudgetExceeded

    plan = Plan(task_prompt="test")
    plan.steps.append(Step(index=1, prompt="p", description="d"))

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_rwt:
        mock_rwt.side_effect = BudgetExceeded("daily", 30.0, 25.0)
        with pytest.raises(BudgetExceeded):
            await _opus_verify("test task", ["output"], plan)


# ---------------------------------------------------------------------------
# Test: learning is loaded and injected during first-run planning (integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_task_loads_learning_before_plan(eng):
    """On first run (no existing steps), run_task loads learning context and
    passes it to _opus_plan. The plan prompt received by opus contains 'PRIOR
    ATTEMPTS' when there are prior failures in the DB."""
    from backend.database import TaskOutcome

    _seed_state(eng)
    task_id = _seed_task(eng)

    # Seed a prior failure so learning context is non-empty.
    with Session(eng) as s:
        s.add(TaskOutcome(
            task_id=99,
            verdict="failure",
            confidence=0.9,
            reason="prior task failed spectacularly",
            grounded=False,
        ))
        s.commit()

    plan_prompts = []

    async def fake_opus(model, prompt, *args, **kwargs):
        plan_prompts.append(prompt)
        return '{"steps": [{"index": 1, "description": "step", "prompt": "do it"}]}'

    with patch("backend.agents.router.run_model", new=fake_opus), \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.agents.orchestrator._opus_verify", new_callable=AsyncMock) as mock_verify:

        mock_exec.return_value = "done"
        mock_verify.return_value = _success_verify_outcome()

        from backend.agents.orchestrator import run_task
        result = await run_task("task", task_id)

    assert result.success is True
    # The plan prompt (first opus call) must contain the learning block.
    assert len(plan_prompts) >= 1
    assert "PRIOR ATTEMPTS" in plan_prompts[0]
    assert "prior task failed spectacularly" in plan_prompts[0]
