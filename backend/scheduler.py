import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(
    job_defaults={
        "coalesce": True,        # collapse a backlog of missed runs into one
        "misfire_grace_time": 30,  # tolerate up to 30s of loop stall before skipping
        "max_instances": 1,      # never run two copies of the same job concurrently
    }
)


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


async def _record_uptime():
    try:
        from sqlmodel import Session
        from backend.database import UptimeSample, engine
        from backend.integrations import (
            adguard, channels_dvr, github, hermes, homeassistant,
            obsidian, openrouter, unifi, unraid, weather,
        )
        import time

        sources = {
            "homeassistant": homeassistant, "unifi": unifi, "unraid": unraid,
            "obsidian": obsidian, "github": github, "openrouter": openrouter,
            "weather": weather, "channels_dvr": channels_dvr, "adguard": adguard,
            "hermes": hermes,
        }

        async def _check(name, mod):
            t0 = time.monotonic()
            try:
                ok = await mod.health_check()
            except Exception:
                ok = False
            ms = int((time.monotonic() - t0) * 1000)
            return name, bool(ok), ms

        # Run checks SEQUENTIALLY, not concurrently. Firing all 10 at once thunders
        # the event loop with cold TLS handshakes: some false-fail on their 2s
        # timeout and the survivors report inflated latency that is really
        # event-loop queue time, not network time. One-at-a-time gives accurate
        # reachability + latency. 10 checks every 2 min is cheap.
        results = []
        for n, m in sources.items():
            results.append(await _check(n, m))

        with Session(engine) as session:
            for name, ok, ms in results:
                session.add(UptimeSample(source=name, ok=ok, latency_ms=ms))
            session.commit()
        logger.info(f"Uptime recorded: {sum(1 for _, ok, _ in results if ok)}/{len(results)} up")
    except Exception as e:
        logger.error(f"Uptime record error: {e}")


async def _record_speedtest():
    try:
        from sqlmodel import Session
        from backend.database import SpeedtestSample, engine
        from backend.integrations.speedtest import run_speedtest

        result = await run_speedtest()
        with Session(engine) as session:
            session.add(SpeedtestSample(
                download_mbps=result.get("download_mbps", 0.0),
                upload_mbps=result.get("upload_mbps", 0.0),
                ping_ms=result.get("ping_ms", 0.0),
            ))
            session.commit()
        logger.info(f"Speedtest recorded: {result}")
    except Exception as e:
        logger.error(f"Speedtest record error: {e}")


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
    scheduler.add_job(
        _record_uptime,
        IntervalTrigger(minutes=2),
        id="record_uptime",
        replace_existing=True,
    )
    scheduler.add_job(
        _record_speedtest,
        IntervalTrigger(minutes=30),
        id="record_speedtest",
        replace_existing=True,
    )
    logger.info(f"Scheduler configured: briefing at {briefing_time} {timezone}")
