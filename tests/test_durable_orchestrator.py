import json

import pytest
from unittest.mock import AsyncMock, patch

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


def _seed_task(eng, prompt="task"):
    from backend.database import Task

    with Session(eng) as s:
        t = Task(prompt=prompt, status="pending")
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

    with patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_sonnet:
        mock_sonnet.side_effect = ["OUT2", "OUT3"]
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is True
    assert mock_sonnet.await_count == 2

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

    async def sonnet_side(prompt, system=""):
        # When the second step runs, the first step must already be 'done'.
        with Session(eng) as s:
            rows = s.exec(select(TaskStep).where(TaskStep.task_id == task_id).order_by(TaskStep.step_index)).all()
            seen_after_first[len(seen_after_first)] = [r.status for r in rows]
        return "ok"

    with patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=sonnet_side):
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

    with patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_sonnet:
        from backend.agents.orchestrator import run_task

        result = await run_task("task", task_id)

    assert result.success is False
    assert result.reason == "cancelled"
    mock_sonnet.assert_not_awaited()

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

    async def sonnet_side(prompt, system=""):
        call["n"] += 1
        if call["n"] == 1:
            return "I cannot complete this task"
        return "fixed"

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=sonnet_side):
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

    async def sonnet_side(prompt, system=""):
        call["n"] += 1
        if call["n"] == 1:
            return "I cannot complete this task"  # bad-step fails -> replan
        return "good"

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=sonnet_side):
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


@pytest.mark.asyncio
async def test_no_id_legacy_path(eng):
    """task_id=None uses the legacy in-memory loop (results reset each retry)."""
    plan_json = '{"steps": [{"index": 1, "description": "s", "prompt": "p"}]}'

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_sonnet, \
         patch("sqlmodel.Session"):
        mock_opus.return_value = plan_json
        mock_sonnet.return_value = "done"
        from backend.agents.orchestrator import run_task

        result = await run_task("legacy task")

    assert result.success is True
    assert result.output == ["done"]
    # No TaskStep rows created for the legacy path.
    from backend.database import TaskStep

    with Session(eng) as s:
        assert s.exec(select(TaskStep)).all() == []
