"""Daily autonomy digest (Tier 3 phone notifications).

Builds and delivers a concise summary of what NEXUS ran autonomously, what is
awaiting human approval, today's spend, and the current autonomy state.

Sent once daily via events.notify_phone (-> Hermes -> Telegram).

Best-effort throughout: any sub-fetch failure degrades that line to a safe
default; the function NEVER raises.  All sync DB helpers are invoked via
asyncio.to_thread — no Session/ORM crosses an await boundary.
"""
import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync DB helpers — call ONLY via asyncio.to_thread
# ---------------------------------------------------------------------------

def _db_recent_autonomous_goals(since: datetime) -> list[dict]:
    """Goals auto-approved in the last 24 h (approved_by=="auto:low_risk_reversible")."""
    from sqlmodel import Session, select
    from backend.database import Goal, engine

    try:
        with Session(engine) as session:
            rows = session.exec(
                select(Goal)
                .where(Goal.approved_by == "auto:low_risk_reversible")
                .where(Goal.updated_at >= since)
                .order_by(Goal.updated_at.desc())
                .limit(20)
            ).all()
            return [{"title": r.title, "status": r.status} for r in rows]
    except Exception as e:
        logger.debug(f"digest._db_recent_autonomous_goals failed: {e}")
        return []


def _db_pending_confirm_count() -> int:
    """Count of ActionLog rows with decision == 'needs_confirm'."""
    from sqlmodel import Session, func, select
    from backend.database import ActionLog, engine

    try:
        with Session(engine) as session:
            count = session.exec(
                select(func.count(ActionLog.id))
                .where(ActionLog.decision == "needs_confirm")
            ).one()
            return int(count or 0)
    except Exception as e:
        logger.debug(f"digest._db_pending_confirm_count failed: {e}")
        return 0


def _db_proposed_goals() -> list[dict]:
    """Goals with status == 'proposed', newest first, up to 10."""
    from sqlmodel import Session, select
    from backend.database import Goal, engine

    try:
        with Session(engine) as session:
            rows = session.exec(
                select(Goal)
                .where(Goal.status == "proposed")
                .order_by(Goal.created_at.desc())
                .limit(10)
            ).all()
            return [{"title": r.title, "risk": r.risk} for r in rows]
    except Exception as e:
        logger.debug(f"digest._db_proposed_goals failed: {e}")
        return []


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------

async def build_autonomy_digest() -> str:
    """Compute and format the daily autonomy digest text.

    Best-effort: any sub-fetch failure degrades that line to a safe default.
    Never raises.
    """
    try:
        since = datetime.utcnow() - timedelta(hours=24)
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

        # Fan-out all DB reads + governor calls concurrently.
        from backend.safety import governor

        auto_goals_task = asyncio.to_thread(_db_recent_autonomous_goals, since)
        pending_count_task = asyncio.to_thread(_db_pending_confirm_count)
        proposed_goals_task = asyncio.to_thread(_db_proposed_goals)
        spend_task = asyncio.to_thread(governor.today_spend_usd)
        state_task = asyncio.to_thread(governor.get_system_state)

        results = await asyncio.gather(
            auto_goals_task,
            pending_count_task,
            proposed_goals_task,
            spend_task,
            state_task,
            return_exceptions=True,
        )

        auto_goals = results[0] if not isinstance(results[0], Exception) else []
        pending_count = results[1] if not isinstance(results[1], Exception) else 0
        proposed_goals = results[2] if not isinstance(results[2], Exception) else []
        spend = results[3] if not isinstance(results[3], Exception) else 0.0
        state = results[4] if not isinstance(results[4], Exception) else {}

        autonomy_label = "ENABLED" if state.get("autonomy_enabled", True) else "PAUSED"
        daily_cap = state.get("daily_budget_usd", 25.0)

        # Format auto-ran goals line.
        if auto_goals:
            goal_parts = ", ".join(
                f"{g['title']} ({g['status']})" for g in auto_goals[:5]
            )
            auto_ran_line = f"{len(auto_goals)} — {goal_parts}"
        else:
            auto_ran_line = "none"

        # Format proposed goals block.
        if proposed_goals:
            proposed_titles = "\n    - " + "\n    - ".join(
                g["title"] for g in proposed_goals[:5]
            )
        else:
            proposed_titles = "\n    (none)"

        lines = [
            f"NEXUS autonomy digest — {date_str}",
            f"Autonomy: {autonomy_label}",
            f"Auto-ran (24h): {auto_ran_line}",
            f"Awaiting your approval: {pending_count} action(s) + {len(proposed_goals)} proposed goal(s)",
            f"  proposed:{proposed_titles}",
            f"Spend today: ${spend:.2f} / ${daily_cap:.2f}",
        ]
        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"build_autonomy_digest failed (best-effort): {e}")
        return f"NEXUS autonomy digest — {datetime.utcnow().strftime('%Y-%m-%d')}\n(digest unavailable: {e})"


async def send_autonomy_digest() -> dict:
    """Build and deliver the daily autonomy digest via phone notification.

    Never raises. Returns {"delivered": bool, "text": str}.
    """
    try:
        from backend import events
        text = await build_autonomy_digest()
        delivered = await events.notify_phone(text, kind="autonomy_digest")
        return {"delivered": delivered, "text": text}
    except Exception as e:
        logger.debug(f"send_autonomy_digest failed (best-effort): {e}")
        return {"delivered": False, "text": ""}
