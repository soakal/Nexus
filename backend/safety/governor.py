"""Cost governor + global kill switch (Tier 1.5).

Two safety mechanisms backed by the DB:

  * A SPENDING GOVERNOR — sums `SpendLog.cost_usd` over time windows and raises
    `BudgetExceeded` when a daily or per-task cap is reached. The router checks the
    daily cap before every billed LLM call (`_run`); the orchestrator checks the
    per-task cap between steps of a durable task.
  * A GLOBAL KILL SWITCH — `SystemState.autonomy_enabled`. When False the action
    broker forbids agent/autonomous side effects (user actions are unaffected).

Every function here is SYNCHRONOUS and opens/closes its own Session. Async callers
MUST invoke them via `asyncio.to_thread` so no Session/ORM crosses an `await`
(Windows ProactorEventLoop safety — see CLAUDE.md). The router's universal brake
and the orchestrator's per-task brake both wrap these in `asyncio.to_thread`.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Literal fallbacks if SystemState is missing — kept in sync with config defaults.
_DEFAULT_DAILY_BUDGET_USD = 25.0
_DEFAULT_PER_TASK_BUDGET_USD = 5.0
_DEFAULT_AUTONOMY_ENABLED = True


class BudgetExceeded(Exception):
    """Raised when a spending cap is reached.

    `scope` is "daily" or "per_task". `spend` is the dollars spent in that window,
    `cap` the configured limit, and `task_id` the task that tripped a per-task cap
    (None for the daily cap).
    """

    def __init__(self, scope: str, spend: float, cap: float, task_id: int | None = None):
        self.scope = scope
        self.spend = spend
        self.cap = cap
        self.task_id = task_id
        super().__init__(
            f"Budget exceeded ({scope}): spent ${spend:.4f} >= cap ${cap:.4f}"
            + (f" (task {task_id})" if task_id is not None else "")
        )


def _local_midnight_utc_naive() -> datetime:
    """The most recent local midnight (in briefing_timezone) as a NAIVE UTC instant.

    SpendLog.created_at is stored as a naive `datetime.utcnow()` value, so we must
    compare against a naive UTC datetime. We compute local midnight in the user's
    configured timezone, then convert that instant to UTC and drop the tzinfo.
    """
    from zoneinfo import ZoneInfo

    try:
        from backend.config import get_settings
        tzname = get_settings().briefing_timezone
    except Exception:
        tzname = "UTC"
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = ZoneInfo("UTC")

    now_local = datetime.now(tz)
    local_midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert local-midnight instant to UTC, then strip tzinfo to match naive utcnow.
    utc_midnight = local_midnight.astimezone(ZoneInfo("UTC"))
    return utc_midnight.replace(tzinfo=None)


def today_spend_usd() -> float:
    """Sum of SpendLog.cost_usd for the current local day (sync)."""
    from sqlmodel import Session, func, select

    from backend.database import SpendLog, engine

    since = _local_midnight_utc_naive()
    with Session(engine) as session:
        total = session.exec(
            select(func.coalesce(func.sum(SpendLog.cost_usd), 0.0))
            .where(SpendLog.created_at >= since)
        ).one()
    return float(total or 0.0)


def task_spend_since(task_start: datetime) -> float:
    """Sum of SpendLog.cost_usd recorded since `task_start` (sync)."""
    from sqlmodel import Session, func, select

    from backend.database import SpendLog, engine

    with Session(engine) as session:
        total = session.exec(
            select(func.coalesce(func.sum(SpendLog.cost_usd), 0.0))
            .where(SpendLog.created_at >= task_start)
        ).one()
    return float(total or 0.0)


def get_system_state() -> dict:
    """Read the SystemState row (id=1) as a plain dict (sync).

    Returns safe defaults if the row is missing so callers never crash.
    """
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            return {
                "autonomy_enabled": _DEFAULT_AUTONOMY_ENABLED,
                "daily_budget_usd": _DEFAULT_DAILY_BUDGET_USD,
                "per_task_budget_usd": _DEFAULT_PER_TASK_BUDGET_USD,
            }
        return {
            "autonomy_enabled": bool(row.autonomy_enabled),
            "daily_budget_usd": float(row.daily_budget_usd),
            "per_task_budget_usd": float(row.per_task_budget_usd),
        }


def set_autonomy(enabled: bool) -> None:
    """Flip the global kill switch on the SystemState row (sync)."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)
        row.autonomy_enabled = bool(enabled)
        row.updated_at = datetime.utcnow()
        session.commit()


def set_budgets(daily: float | None = None, per_task: float | None = None) -> None:
    """Update the runtime budget caps on the SystemState row (sync)."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)
        if daily is not None:
            row.daily_budget_usd = float(daily)
        if per_task is not None:
            row.per_task_budget_usd = float(per_task)
        row.updated_at = datetime.utcnow()
        session.commit()


def check_budget(task_id: int | None = None, task_start: datetime | None = None) -> None:
    """Raise BudgetExceeded if a cap is reached, else return None (sync).

    Always checks the daily cap. If `task_start` is given, also checks the per-task
    cap for spend since that instant.
    """
    state = get_system_state()

    daily = today_spend_usd()
    if daily >= state["daily_budget_usd"]:
        raise BudgetExceeded("daily", daily, state["daily_budget_usd"])

    if task_start is not None:
        ts = task_spend_since(task_start)
        if ts >= state["per_task_budget_usd"]:
            raise BudgetExceeded("per_task", ts, state["per_task_budget_usd"], task_id)

    return None
