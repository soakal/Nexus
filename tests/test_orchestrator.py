import pytest
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Tier 2.1 migration: the executor no longer goes through router.sonnet with
# WEB_SEARCH/VAULT_SEARCH regex directives — it now calls
# router.run_with_tools (native read-only tool-use loop). These four legacy
# cases are mechanically migrated to patch backend.agents.router.run_with_tools
# instead of backend.agents.router.sonnet; the canned return strings (and the
# opus plan/debug JSON) are unchanged, so the orchestrator control flow under
# test is identical.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path():
    plan_json = '{"steps": [{"index": 1, "description": "say hello", "prompt": "say hello"}, {"index": 2, "description": "confirm", "prompt": "confirm"}]}'

    with patch("backend.agents.router.run_model", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_opus.return_value = plan_json
        mock_exec.return_value = "Step completed successfully"

        from backend.agents.orchestrator import run_task
        result = await run_task("Test task")
        assert result.success is True
        assert len(result.output) >= 1


@pytest.mark.asyncio
async def test_failure_triggers_debug():
    plan_json = '{"steps": [{"index": 1, "description": "do thing", "prompt": "do thing"}]}'
    debug_json = '{"action": "ABORT", "reason": "Cannot complete"}'

    with patch("backend.agents.router.run_model", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_opus.side_effect = [plan_json, debug_json]
        mock_exec.return_value = "I cannot complete this task"

        from backend.agents.orchestrator import run_task
        result = await run_task("Failing task")
        assert result.success is False


@pytest.mark.asyncio
async def test_retry_step():
    plan_json = '{"steps": [{"index": 1, "description": "step", "prompt": "try"}]}'
    retry_json = '{"action": "RETRY_STEP", "reason": "try again", "new_prompt": "try differently"}'

    call_count = 0
    async def exec_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "I cannot complete this task"
        return "Done successfully"

    with patch("backend.agents.router.run_model", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock, side_effect=exec_side_effect), \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_opus.side_effect = [plan_json, retry_json]

        from backend.agents.orchestrator import run_task
        result = await run_task("Retry task")
        # Should have attempted a retry
        assert call_count >= 2


@pytest.mark.asyncio
async def test_replan():
    plan_json = '{"steps": [{"index": 1, "description": "step", "prompt": "try"}]}'
    replan_json = f'{{"action": "REPLAN", "reason": "needs replanning", "new_plan": {{"steps": [{{"index": 1, "description": "new step", "prompt": "different approach"}}]}}}}'

    with patch("backend.agents.router.run_model", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_opus.side_effect = [plan_json, replan_json, plan_json]
        mock_exec.side_effect = ["I cannot complete this task", "Done after replan"]

        from backend.agents.orchestrator import run_task
        result = await run_task("Replan task")
        # Should have tried replanning
        assert mock_opus.call_count >= 2


# ---------------------------------------------------------------------------
# Tier 2.1 NEW: parity + budget propagation through the durable path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_uses_run_with_tools():
    """_sonnet_execute delegates to router.run_with_tools with the read-only
    tool set + hosted web search, NOT to router.sonnet."""
    from backend.agents.orchestrator import Step, _sonnet_execute

    with patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = "answer"
        out = await _sonnet_execute(Step(index=1, prompt="do it", description="d"), [])
        assert out == "answer"
        assert mock_exec.call_count == 1
        kwargs = mock_exec.call_args.kwargs
        assert kwargs["web_search"] is True
        assert kwargs["label"] == "orchestrator_execute"
        # The read-only tool registry is wired in.
        names = {s["name"] for s in kwargs["tool_specs"]}
        # ITEM 5: the local DuckDuckGo tool is now named ddg_search (the hosted
        # web_search is added separately via web_search=True, not in tool_specs).
        assert "ddg_search" in names and "vault_search" in names
        assert "do it" in kwargs["prompt"]


@pytest.mark.asyncio
async def test_opus_plan_uses_planner_tool_block():
    """The planner prompt advertises the native read-only tools and no longer
    carries the old MANDATORY RULES / WEB_SEARCH:/VAULT_SEARCH: directive text."""
    from backend.agents.tools import planner_tool_block

    captured = {}

    async def fake_run_model(model, prompt, *a, **k):
        captured["prompt"] = prompt
        return '{"steps": [{"index": 1, "description": "d", "prompt": "p"}]}'

    with patch("backend.agents.router.run_model", new=fake_run_model):
        from backend.agents.orchestrator import _opus_plan
        await _opus_plan("some task")

    prompt = captured["prompt"]
    assert planner_tool_block() in prompt
    assert "MANDATORY RULES" not in prompt
    assert "WEB_SEARCH:" not in prompt
    assert "VAULT_SEARCH:" not in prompt
    assert "they call them natively" in prompt or "calls them natively" in prompt


@pytest.mark.asyncio
async def test_budget_exceeded_mid_step_durable(tmp_path):
    """A BudgetExceeded raised by the per-task brake mid-task finalizes the task
    failed/budget_exceeded via the durable path (task_id set)."""
    import backend.database as database
    from sqlmodel import Session, create_engine, SQLModel, select

    # Isolated on-disk SQLite so the durable helpers have real tables.
    db_path = tmp_path / "nexus_test.db"
    test_engine = create_engine(f"sqlite:///{db_path}")
    SQLModel.metadata.create_all(test_engine)

    from backend.database import Task, TaskStep
    with Session(test_engine) as s:
        t = Task(prompt="Budget task", status="pending")
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id

    plan_json = '{"steps": [{"index": 1, "description": "step one", "prompt": "p1"}, {"index": 2, "description": "step two", "prompt": "p2"}]}'

    from backend.safety.governor import BudgetExceeded

    call_count = 0

    def fake_check_budget(task_id=None, task_start=None):
        nonlocal call_count
        call_count += 1
        # Allow the first step's brake, trip on the second.
        if call_count >= 2:
            raise BudgetExceeded("per_task", spend=9.0, cap=5.0, task_id=task_id)

    with patch.object(database, "engine", test_engine), \
         patch("backend.agents.router.run_model", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_exec, \
         patch("backend.safety.governor.check_budget", side_effect=fake_check_budget):

        mock_opus.return_value = plan_json
        mock_exec.return_value = "Step completed successfully"

        from backend.agents.orchestrator import run_task
        result = await run_task("Budget task", task_id=task_id)

    assert result.success is False
    assert result.reason == "budget_exceeded"

    with Session(test_engine) as s:
        t = s.get(Task, task_id)
        assert t.status == "failed"
        import json
        rj = json.loads(t.result_json)
        assert rj["error"] == "budget_exceeded"
        assert rj["scope"] == "per_task"
        # First step checkpointed (done) before the budget tripped — preserved.
        done = s.exec(
            select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.status == "done")
        ).all()
        assert len(done) == 1
