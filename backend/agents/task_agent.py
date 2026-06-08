import logging

from backend.agents.orchestrator import TaskResult, run_task

logger = logging.getLogger(__name__)


async def execute_task(prompt: str, task_id: int | None = None) -> TaskResult:
    return await run_task(prompt, task_id)
