import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def _run_briefing():
    try:
        from backend.agents.briefing import run_briefing
        await run_briefing()
    except Exception as e:
        logger.error(f"Briefing job error: {e}")


async def _snapshot_trends():
    try:
        import asyncio

        from sqlmodel import Session

        from backend.database import TrendSnapshot, engine
        from backend.integrations.adguard import fetch as fetch_adguard
        from backend.integrations.channels_dvr import fetch as fetch_channels
        from backend.integrations.unraid import fetch as fetch_unraid

        results = await asyncio.gather(
            fetch_unraid(), fetch_channels(), fetch_adguard(),
            return_exceptions=True
        )
        unraid_data, channels_data, adguard_data = results

        snapshots = []
        if not isinstance(unraid_data, Exception):
            snapshots.append(TrendSnapshot(source="unraid", metric="storage_used_gb", value=unraid_data.storage_used_gb))
        if not isinstance(channels_data, Exception):
            snapshots.append(TrendSnapshot(source="channels", metric="storage_used_gb", value=channels_data.storage_used_gb))
        if not isinstance(adguard_data, Exception):
            snapshots.append(TrendSnapshot(source="adguard", metric="blocked_pct", value=adguard_data.blocked_pct))

        with Session(engine) as session:
            for s in snapshots:
                session.add(s)
            session.commit()
        logger.info(f"Trend snapshot: {len(snapshots)} rows written")
    except Exception as e:
        logger.error(f"Trend snapshot error: {e}")


async def _retry_pending_deliveries():
    try:
        from backend.integrations.hermes import deliver_pending
        await deliver_pending()
    except Exception as e:
        logger.error(f"Retry delivery error: {e}")


def setup_scheduler(briefing_time: str, timezone: str):
    hour, minute = briefing_time.split(":")
    scheduler.add_job(
        _run_briefing,
        CronTrigger(hour=int(hour), minute=int(minute), timezone=timezone),
        id="morning_briefing",
        replace_existing=True,
    )
    scheduler.add_job(
        _snapshot_trends,
        IntervalTrigger(minutes=15),
        id="trend_snapshots",
        replace_existing=True,
    )
    scheduler.add_job(
        _retry_pending_deliveries,
        IntervalTrigger(seconds=60),
        id="retry_deliveries",
        replace_existing=True,
    )
    logger.info(f"Scheduler configured: briefing at {briefing_time} {timezone}")
