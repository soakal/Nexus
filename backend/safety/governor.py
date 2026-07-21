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


def _local_now() -> datetime:
    """Now in the user's configured timezone (briefing_timezone, UTC fallback)."""
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
    return datetime.now(tz)


def _local_today_date_str() -> str:
    """Today's date (ISO, e.g. "2026-07-20") in the user's configured timezone.

    Shared by budget_warning_due — the day IS the edge for that check, so a
    date string (not a timestamp) makes rollover reset free."""
    return _local_now().date().isoformat()


def _local_midnight_utc_naive() -> datetime:
    """The most recent local midnight (in briefing_timezone) as a NAIVE UTC instant.

    SpendLog.created_at is stored as a naive `datetime.utcnow()` value, so we must
    compare against a naive UTC datetime. We compute local midnight in the user's
    configured timezone, then convert that instant to UTC and drop the tzinfo.
    """
    from zoneinfo import ZoneInfo

    now_local = _local_now()
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


def task_spend_since(task_start: datetime, task_id: int | None = None) -> float:
    """Sum of SpendLog.cost_usd recorded since `task_start` (sync).

    When `task_id` is given, the sum is additionally scoped to rows tagged with
    that task_id — so one task's spend can never trip another task's per-task cap.
    With `task_id=None` the behaviour is unchanged (time-window only).
    """
    from sqlmodel import Session, func, select

    from backend.database import SpendLog, engine

    with Session(engine) as session:
        query = (
            select(func.coalesce(func.sum(SpendLog.cost_usd), 0.0))
            .where(SpendLog.created_at >= task_start)
        )
        if task_id is not None:
            query = query.where(SpendLog.task_id == task_id)
        total = session.exec(query).one()
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


def _today_spend_row_count() -> int:
    """Count SpendLog rows recorded since the most recent local midnight (sync).

    Uses the same window logic as `today_spend_usd()` so the two numbers are
    directly comparable (row count vs. dollar sum over the same window).
    """
    from sqlmodel import Session, func, select

    from backend.database import SpendLog, engine

    since = _local_midnight_utc_naive()
    with Session(engine) as session:
        count = session.exec(
            select(func.count(SpendLog.id)).where(SpendLog.created_at >= since)
        ).one()
    return int(count or 0)


def metering_health() -> dict:
    """Return a dict summarising live metering health (sync).

    Intended for the GET /api/safety/metering endpoint. All fields are
    best-effort: today_spend_usd / today_row_count are current as of this call;
    counters are process-lifetime (reset on restart).
    """
    from backend.agents.router import metering_counters
    from backend.config import get_settings

    return {
        "counters": metering_counters(),
        "today_spend_usd": today_spend_usd(),
        "today_row_count": _today_spend_row_count(),
        "prices_verified": bool(getattr(get_settings(), "prices_verified", False)),
    }


def spend_report(days: int = 7) -> dict:
    """Return a per-model spend breakdown for the last `days` days (sync).

    Groups SpendLog rows by model over the window, sorts by cost descending,
    and surfaces whether prices have been field-verified. Best-effort: any DB
    error returns a safe error dict with empty by_model.

    Callers MUST invoke via asyncio.to_thread — this opens its own Session.
    """
    from datetime import timedelta

    prices_verified_flag = False
    try:
        from backend.config import get_settings
        prices_verified_flag = bool(getattr(get_settings(), "prices_verified", False))
    except Exception:
        pass

    try:
        from sqlmodel import Session, func, select
        from backend.database import SpendLog, engine

        cutoff = datetime.utcnow() - timedelta(days=days)

        with Session(engine) as session:
            rows = session.exec(
                select(SpendLog).where(SpendLog.created_at >= cutoff)
            ).all()

        # Group by model in Python (SQLite GROUP BY + multiple aggregates is
        # simpler here and the row count is small).
        model_map: dict[str, dict] = {}
        for row in rows:
            m = row.model or "unknown"
            if m not in model_map:
                model_map[m] = {
                    "model": m,
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                }
            model_map[m]["calls"] += 1
            model_map[m]["input_tokens"] += int(row.input_tokens or 0)
            model_map[m]["output_tokens"] += int(row.output_tokens or 0)
            model_map[m]["cost_usd"] += float(row.cost_usd or 0.0)

        # Parallel grouping by label — turns the weekly report into a tuning
        # tool ("which call site costs the most"). "" -> "(unlabeled)".
        label_map: dict[str, dict] = {}
        for row in rows:
            lb = row.label or "(unlabeled)"
            if lb not in label_map:
                label_map[lb] = {
                    "label": lb,
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_usd": 0.0,
                }
            label_map[lb]["calls"] += 1
            label_map[lb]["input_tokens"] += int(row.input_tokens or 0)
            label_map[lb]["output_tokens"] += int(row.output_tokens or 0)
            label_map[lb]["cost_usd"] += float(row.cost_usd or 0.0)

        by_model = sorted(model_map.values(), key=lambda x: x["cost_usd"], reverse=True)
        by_label = sorted(label_map.values(), key=lambda x: x["cost_usd"], reverse=True)
        total_usd = sum(e["cost_usd"] for e in by_model)
        total_calls = sum(e["calls"] for e in by_model)

        return {
            "days": days,
            "since": cutoff.isoformat(),
            "by_model": by_model,
            "by_label": by_label,
            "total_usd": total_usd,
            "total_calls": total_calls,
            "prices_verified": prices_verified_flag,
        }

    except Exception as e:
        logger.warning(f"governor.spend_report failed (best-effort): {e}")
        return {
            "days": days,
            "by_model": [],
            "by_label": [],
            "total_usd": 0.0,
            "total_calls": 0,
            "prices_verified": prices_verified_flag,
            "error": str(e),
        }


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
        ts = task_spend_since(task_start, task_id)
        if ts >= state["per_task_budget_usd"]:
            raise BudgetExceeded("per_task", ts, state["per_task_budget_usd"], task_id)

    return None


def budget_warning_due(threshold_pct: float) -> tuple[bool, float, float]:
    """Whether an 80%-of-daily-cap early warning should fire right now (sync).

    Returns (due, spend, cap). Fires at most once per local day — claims the
    day (writes SystemState.last_budget_warn_day) in the same call that
    returns True, mirroring watchdog._should_alert_dead_letters_db's
    claim-before-notify pattern (a failed notify still consumes the day; the
    same accepted tradeoff as the dead-letter alert).

    A threshold_pct outside (0, 1) is treated as 0.80 (defensive — degrade
    rather than crash on a bad config value). cap <= 0 never fires (no
    division, no warning). Creates SystemState row id=1 if it doesn't exist
    yet, same as set_autonomy.
    """
    if not (0 < threshold_pct < 1):
        threshold_pct = 0.80

    from sqlmodel import Session
    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)
            session.commit()
            session.refresh(row)

        cap = float(row.daily_budget_usd)
        spend = today_spend_usd()

        if cap <= 0:
            return (False, spend, cap)

        today_str = _local_today_date_str()
        if spend >= threshold_pct * cap and row.last_budget_warn_day != today_str:
            row.last_budget_warn_day = today_str
            row.updated_at = datetime.utcnow()
            session.add(row)
            session.commit()
            return (True, spend, cap)

        return (False, spend, cap)
