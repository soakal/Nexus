import asyncio
import json

import pytest
from unittest.mock import AsyncMock, patch

# Tier 2.1: the executor (_sonnet_execute) now calls router.run_with_tools (the
# native read-only tool-use loop) instead of router.sonnet. These tests are
# mechanically migrated to patch run_with_tools with the SAME canned strings;
# the orchestrator control flow under test (resume/checkpoint/cancel/retry/
# replan) is unchanged. One run_with_tools await == one executed step, exactly
# as one sonnet await did before.

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

# Import the models module so every table is registered on SQLModel.metadata
# before create_all runs (otherwise the first test's engine gets no tables).
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


@pytest.mark.asyncio
async def test_resume_skips_done_steps(eng):
    """KEYSTONE: a task with [done, pending, pending] resumes — sonnet runs
    exactly twice, result reflects all three outputs."""
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "step one", status="done", output="OUT1")
    _seed_step(eng, task_id, 2, "step two", status="pending")
    _seed_step(eng, task_id, 3, "step three", status="pending")

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        mock_exec.side_effect = ["OUT2", "OUT3"]
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is True
    assert mock_exec.await_count == 2

    from backend.database import Task

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "success"
        outputs = json.loads(t.result_json)
        assert outputs == ["OUT1", "OUT2", "OUT3"]


@pytest.mark.asyncio
async def test_checkpoint_persisted_each_step(eng):
    """Each step output is committed to its TaskStep row the instant it finishes."""
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "a", status="pending")
    _seed_step(eng, task_id, 2, "b", status="pending")

    from backend.database import TaskStep

    seen_after_first = {}

    async def exec_side(*args, **kwargs):
        # When the second step runs, the first step must already be 'done'.
        with Session(eng) as s:
            rows = s.exec(select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.step_index)).all()
            seen_after_first[len(seen_after_first)] = [r.status for r in rows]
        return "ok"

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock, side_effect=exec_side):
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is True
    # First sonnet call: step1 running, step2 pending. Second call: step1 done.
    assert seen_after_first[1][0] == "done"


@pytest.mark.asyncio
async def test_cooperative_cancel_marks_stopped(eng):
    """cancel_requested between steps -> Task 'stopped', no further sonnet, done
    steps preserved."""
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "a", status="done", output="A")
    _seed_step(eng, task_id, 2, "b", status="pending")

    from backend.database import Task

    with Session(eng) as s:
        t = s.get(Task, task_id)
        t.cancel_requested = True
        s.commit()

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is False
    assert result.reason == "cancelled"
    mock_exec.assert_not_awaited()

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "stopped"
        from backend.database import TaskStep
        step1 = s.exec(select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step_index == 1)).first()
        assert step1.status == "done"


@pytest.mark.asyncio
async def test_retry_step_durable(eng):
    """A failing step triggers RETRY_STEP, which durably patches that one row and
    re-runs it to success."""
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "try", status="pending")

    retry_json = '{"action": "RETRY_STEP", "reason": "x", "new_prompt": "try harder"}'

    call = {"n": 0}

    async def exec_side(*args, **kwargs):
        call["n"] += 1
        if call["n"] == 1:
            return "I cannot complete this task"
        return "fixed"

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock, side_effect=exec_side):
        mock_opus.return_value = retry_json
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is True

    from backend.database import TaskStep

    with Session(eng) as s:
        step = s.exec(select(TaskStep).where(TaskStep.task_id == task_id)).first()
        assert step.prompt == "try harder"
        assert step.status == "done"
        assert step.attempts >= 2  # ran twice, attempts preserved across patch


@pytest.mark.asyncio
async def test_replan_changes_step_count(eng):
    """A REPLAN that emits a different number of steps yields unique, contiguous
    step_index rows (done rows kept, new ones indexed after max done)."""
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "ok-step", status="done", output="A")
    _seed_step(eng, task_id, 2, "bad-step", status="pending")

    replan = json.dumps({
        "action": "REPLAN",
        "reason": "redo",
        "new_plan": {"steps": [
            {"index": 1, "description": "n1", "prompt": "new1"},
            {"index": 2, "description": "n2", "prompt": "new2"},
            {"index": 3, "description": "n3", "prompt": "new3"},
        ]},
    })

    call = {"n": 0}

    async def exec_side(*args, **kwargs):
        call["n"] += 1
        if call["n"] == 1:
            return "I cannot complete this task"  # bad-step fails -> replan
        return "good"

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock, side_effect=exec_side):
        mock_opus.return_value = replan
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is True

    from backend.database import TaskStep

    with Session(eng) as s:
        rows = s.exec(select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.step_index)).all()
        indices = [r.step_index for r in rows]
        assert len(indices) == len(set(indices))  # unique
        # done row 1 kept; 3 new pending->done indexed 2,3,4
        assert indices == [1, 2, 3, 4]
        assert rows[0].status == "done" and rows[0].step_index == 1


# ---------------------------------------------------------------------------
# ITEM 2 — kill switch / budget / cancel enforced BETWEEN steps (durable path).
# ---------------------------------------------------------------------------

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


@pytest.mark.asyncio
async def test_autonomy_disabled_midtask_finalizes_stopped(eng):
    """Autonomy turned OFF before the next step -> task 'stopped' with
    autonomy_disabled, the next step's executor is NEVER called, done preserved."""
    from backend.database import Task, TaskStep

    _seed_state(eng, autonomy=False)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "a", status="done", output="A")
    _seed_step(eng, task_id, 2, "b", status="pending")

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is False
    assert result.reason == "stopped"
    mock_exec.assert_not_awaited()  # aborted BEFORE the next step's LLM call

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "stopped"
        assert json.loads(t.result_json)["error"] == "autonomy_disabled"
        step1 = s.exec(
            select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step_index == 1)
        ).first()
        assert step1.status == "done"  # done step preserved


@pytest.mark.asyncio
async def test_per_task_cap_midloop_finalizes_failed(eng):
    """A per-task budget overrun before a step finalizes failed/budget_exceeded."""
    import backend.database as db
    from datetime import datetime, timedelta

    from backend.database import SpendLog, Task

    _seed_state(eng, daily=1000.0, per_task=0.01)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "a", status="pending")

    # Accrue spend tagged to this task, stamped in the future so it is >=task_start.
    with Session(eng) as s:
        s.add(SpendLog(model="claude-sonnet-4-6", cost_usd=5.0, task_id=task_id,
                       created_at=datetime.utcnow() + timedelta(hours=1)))
        s.commit()

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is False
    assert result.reason == "budget_exceeded"
    mock_exec.assert_not_awaited()

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "failed"
        payload = json.loads(t.result_json)
        assert payload["error"] == "budget_exceeded"
        assert payload["scope"] == "per_task"


@pytest.mark.asyncio
async def test_cancel_midloop_inside_tool_loop_stopped(eng):
    """Cancel requested while the tool-use loop is mid-flight -> the loop guard
    raises TaskAborted('cancelled') and the task finalizes 'stopped'/cancelled."""
    from backend.database import Task

    _seed_state(eng, autonomy=True)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "a", status="pending")

    # The executor: simulate a multi-round tool loop that, mid-flight, sets the
    # cancel flag then drives the REAL _loop_guard to observe it. We patch
    # run_with_tools to call the real guard after flipping cancel_requested.
    async def exec_side(*args, **kwargs):
        from backend.agents.router import _loop_guard
        from backend.agents.worker_pool import _set_cancel_requested
        await asyncio.to_thread(_set_cancel_requested, task_id)
        # Now the per-round guard must raise TaskAborted('cancelled').
        await _loop_guard(kwargs["task_id"], kwargs["task_start"])
        return "should not reach"

    with patch("backend.agents.router.run_with_tools", new=exec_side):
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is False
    assert result.reason == "cancelled"
    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "stopped"
        assert json.loads(t.result_json)["error"] == "cancelled"


@pytest.mark.asyncio
async def test_chat_single_shot_unaffected_by_loop_guard(eng):
    """run_with_tools with default task_id=None is inert re: kill/cancel: even with
    autonomy OFF, a single-shot tool call proceeds (only the daily cap applies)."""
    from unittest.mock import MagicMock

    from backend.agents import router

    _seed_state(eng, autonomy=False, daily=1000.0)

    r = MagicMock()
    r.content = [MagicMock(type="text", text="answer")]
    r.stop_reason = "end_turn"
    mock_client = MagicMock()
    mock_client.messages.create.return_value = r

    with patch("anthropic.Anthropic", return_value=mock_client):
        out = await router.run_with_tools(
            model=router.SONNET_MODEL, max_tokens=512, prompt="x",
            system="", tool_specs=[], dispatch={},
        )
    assert out == "answer"  # autonomy OFF did NOT abort a task_id=None call


# ---------------------------------------------------------------------------
# ITEM 3 — poison-step ceiling.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_step_exhausted_at_max_attempts(eng):
    """A step already at MAX_STEP_ATTEMPTS trips step_exhausted WITHOUT executing."""
    from backend.agents.orchestrator import MAX_STEP_ATTEMPTS
    from backend.database import Task, TaskStep

    _seed_state(eng, autonomy=True)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "poison", status="pending")
    # Bump attempts to the ceiling directly.
    with Session(eng) as s:
        step = s.exec(select(TaskStep).where(TaskStep.task_id == task_id)).first()
        step.attempts = MAX_STEP_ATTEMPTS
        s.commit()

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is False
    assert result.reason == "step_exhausted"
    mock_exec.assert_not_awaited()  # exhausted step never executed

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "failed"
        payload = json.loads(t.result_json)
        assert payload["error"] == "step_exhausted"
        assert payload["step_index"] == 1
        assert payload["attempts"] == MAX_STEP_ATTEMPTS


def test_finalize_failed_writes_agent_run(eng):
    """worker_pool._finalize_failed records a minimal AgentRun row (best effort)."""
    from backend.agents.worker_pool import _finalize_failed
    from backend.database import AgentRun, Task

    task_id = _seed_task(eng)
    _finalize_failed(task_id, "boom reason")

    with Session(eng) as s:
        t = s.get(Task, task_id)
        assert t.status == "failed"
        runs = s.exec(select(AgentRun).where(AgentRun.task_id == task_id)).all()
        assert len(runs) == 1
        assert runs[0].agent_type == "orchestrator"
        assert runs[0].success is False
        assert "boom reason" in runs[0].prompt_snippet


def test_terminal_task_not_requeued(eng):
    """_load_unfinished_task_ids returns only pending/running ids — a terminal
    (failed/success/stopped) task is NOT re-enqueued (breaks the poison loop)."""
    from backend.agents.worker_pool import _load_unfinished_task_ids

    pending_id = _seed_task(eng, status="pending")
    running_id = _seed_task(eng, status="running")
    _seed_task(eng, status="failed")
    _seed_task(eng, status="success")
    _seed_task(eng, status="stopped")

    ids = set(_load_unfinished_task_ids())
    assert ids == {pending_id, running_id}


@pytest.mark.asyncio
async def test_boot_resume_normal_task_still_resumes(eng):
    """Sanity: a normal pending step still resumes/executes (the poison guard does
    not block under-ceiling steps)."""
    _seed_state(eng, autonomy=True)
    task_id = _seed_task(eng)
    _seed_step(eng, task_id, 1, "ok", status="pending")

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = "done"
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is True
    mock_exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_id_legacy_path(eng):
    """task_id=None uses the legacy in-memory loop (results reset each retry)."""
    plan_json = '{"steps": [{"index": 1, "description": "s", "prompt": "p"}]}'

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("sqlmodel.Session"):
        mock_opus.return_value = plan_json
        mock_exec.return_value = "done"
        from backend.agents.orchestrator import run_task

        result = await run_task("legacy task")

    assert result.success is True
    assert result.output == ["done"]
    # No TaskStep rows created for the legacy path.
    from backend.database import TaskStep

    with Session(eng) as s:
        assert s.exec(select(TaskStep)).all() == []
