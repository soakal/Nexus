"""Scheduler stall watchdog + Hermes dead-letter alert (Tier 3 blind-spot removal).

Two complementary checks run on a 5-minute schedule:

1. Scheduler stall watchdog — detects individual scheduler jobs whose
   next_run_time is overdue beyond a grace window while the event loop is
   otherwise alive.  NOTE: a TOTAL loop death also kills this watchdog (that
   case is caught by health monitoring); this only catches an individual
   stalled/misfiring job.

2. Hermes dead-letter alert — detects PendingDelivery rows whose attempts
   have reached or exceeded the dead_letter_attempts threshold, meaning Hermes
   has been unreachable for many consecutive retries.

Both checks are BEST-EFFORT (never raise), phone-alert via events.notify_phone,
and debounced per-condition via a process-local in-memory dict so a sustained
outage doesn't spam Telegram every 5 minutes.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Process-local debounce state: maps a string key -> monotonic timestamp of
# last alert sent.  Reset by reset() in tests.
_last_alert: dict[str, float] = {}


def _should_alert(key: str, cooldown_s: float, now: float | None = None) -> bool:
    """Return True (and record the timestamp) if enough time has passed since
    the last alert for *key*.  Passing an explicit *now* makes the logic
    deterministic in tests without sleeping.
    """
    now = now if now is not None else time.monotonic()
    last = _last_alert.get(key, 0.0)
    if now - last >= cooldown_s:
        _last_alert[key] = now
        return True
    return False


def reset() -> None:
    """Clear all debounce state.  Test hook — call at the start of each test."""
    _last_alert.clear()


async def check_scheduler_stalls(*, grace_s: int, cooldown_s: int) -> list[str]:
    """Check every scheduled job for overdue next_run_time.

    Returns the list of stalled job ids (overdue by more than *grace_s*
    seconds).  Fires a phone alert per stalled job (debounced by
    *cooldown_s*).  The watchdog's own job id ("watchdog") is always skipped
    to prevent self-alerting.

    Best-effort: any exception returns [] without propagating.
    """
    try:
        from backend import events
        from backend import scheduler as _sched_mod

        sched = _sched_mod.scheduler
        now_utc = datetime.now(timezone.utc)
        stalled: list[str] = []

        for job in sched.get_jobs():
            # Skip the watchdog's own job and any paused/unscheduled jobs.
            if job.id == "watchdog":
                continue
            if job.next_run_time is None:
                continue

            # Both datetimes are tz-aware; subtraction is safe and correct.
            overdue = (now_utc - job.next_run_time).total_seconds()
            if overdue > grace_s:
                stalled.append(job.id)
                if _should_alert(f"sched:{job.id}", cooldown_s):
                    await events.notify_phone(
                        f"NEXUS scheduler job '{job.id}' is overdue by {int(overdue)}s"
                        " (possible stall).",
                        kind="scheduler_stall",
                    )

        return stalled
    except Exception as exc:
        logger.warning(f"check_scheduler_stalls error (ignored): {exc}")
        return []


def _dead_letter_count(threshold: int) -> list[dict]:
    """Sync helper: query PendingDelivery rows at/above *threshold* attempts.

    Returns a list of dicts with id, delivery_type, attempts.
    Runs via asyncio.to_thread — never called directly from the event loop.
    """
    try:
        from sqlmodel import Session, select
        from backend.database import PendingDelivery, engine

        with Session(engine) as session:
            rows = session.exec(
                select(PendingDelivery).where(PendingDelivery.attempts >= threshold)
            ).all()
            return [
                {"id": r.id, "delivery_type": r.delivery_type, "attempts": r.attempts}
                for r in rows
            ]
    except Exception as exc:
        logger.warning(f"_dead_letter_count error (ignored): {exc}")
        return []


async def check_dead_letters(*, threshold: int, cooldown_s: int) -> int:
    """Check PendingDelivery for rows that have exceeded the retry threshold.

    Returns the count of dead-lettered rows.  Fires a single phone alert
    (debounced by *cooldown_s*) when any are found.

    Best-effort: any exception returns 0 without propagating.
    """
    try:
        from backend import events

        rows = await asyncio.to_thread(_dead_letter_count, threshold)
        if rows:
            logger.error(
                f"{len(rows)} Hermes deliveries dead-lettered (>= {threshold} retries) — "
                "notification pipeline likely broken (check HERMES_WEBHOOK_SECRET / Hermes connectivity)"
            )
        if rows and _should_alert("dead_letters", cooldown_s):
            await events.notify_phone(
                f"NEXUS has {len(rows)} undelivered Hermes message(s) stuck"
                f" (>= {threshold} retries). Check Hermes connectivity.",
                kind="dead_letter",
            )
        return len(rows)
    except Exception as exc:
        logger.warning(f"check_dead_letters error (ignored): {exc}")
        return 0


async def run_watchdog() -> dict:
    """Top-level entry point called by the scheduler every 5 minutes.

    Gated by settings.watchdog_enabled.  Runs both checks and returns a
    summary dict.  NEVER raises — any exception is caught and logged.
    """
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "watchdog_enabled", False):
            return {"skipped": True}

        grace_s = getattr(s, "scheduler_stall_grace_s", 600)
        threshold = getattr(s, "dead_letter_attempts", 5)
        cooldown_s = getattr(s, "watchdog_alert_cooldown_s", 3600)

        stalled = await check_scheduler_stalls(grace_s=grace_s, cooldown_s=cooldown_s)
        dead_count = await check_dead_letters(threshold=threshold, cooldown_s=cooldown_s)

        return {"stalled": stalled, "dead_letters": dead_count}
    except Exception as exc:
        logger.error(f"run_watchdog error (ignored): {exc}")
        return {"stalled": [], "dead_letters": 0}
