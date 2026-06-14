import pytest
from unittest.mock import AsyncMock, patch


@pytest.mark.asyncio
async def test_happy_path():
    plan_json = '{"steps": [{"index": 1, "description": "say hello", "prompt": "say hello"}, {"index": 2, "description": "confirm", "prompt": "confirm"}]}'

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_sonnet, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_opus.return_value = plan_json
        mock_sonnet.return_value = "Step completed successfully"

        from backend.agents.orchestrator import run_task
        result = await run_task("Test task")
        assert result.success is True
        assert len(result.output) >= 1


@pytest.mark.asyncio
async def test_failure_triggers_debug():
    plan_json = '{"steps": [{"index": 1, "description": "do thing", "prompt": "do thing"}]}'
    debug_json = '{"action": "ABORT", "reason": "Cannot complete"}'

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_sonnet, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_opus.side_effect = [plan_json, debug_json]
        mock_sonnet.return_value = "I cannot complete this task"

        from backend.agents.orchestrator import run_task
        result = await run_task("Failing task")
        assert result.success is False


@pytest.mark.asyncio
async def test_retry_step():
    plan_json = '{"steps": [{"index": 1, "description": "step", "prompt": "try"}]}'
    retry_json = '{"action": "RETRY_STEP", "reason": "try again", "new_prompt": "try differently"}'

    call_count = 0
    async def sonnet_side_effect(prompt, system=""):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return "I cannot complete this task"
        return "Done successfully"

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=sonnet_side_effect), \
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
    new_plan = '{"steps": [{"index": 1, "description": "new step", "prompt": "different approach"}]}'
    replan_json = f'{{"action": "REPLAN", "reason": "needs replanning", "new_plan": {{"steps": [{{"index": 1, "description": "new step", "prompt": "different approach"}}]}}}}'

    with patch("backend.agents.router.opus", new_callable=AsyncMock) as mock_opus, \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock) as mock_sonnet, \
         patch("backend.database.engine"), \
         patch("sqlmodel.Session"):

        mock_opus.side_effect = [plan_json, replan_json, plan_json]
        mock_sonnet.side_effect = ["I cannot complete this task", "Done after replan"]

        from backend.agents.orchestrator import run_task
        result = await run_task("Replan task")
        # Should have tried replanning
        assert mock_opus.call_count >= 2
