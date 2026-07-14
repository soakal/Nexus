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


def _parse_uptime_targets() -> list[tuple[str, str, int]]:
    """Parse settings.uptime_http_targets ("name|url|expect,..." — expect
    optional, default 200) into (name, url, expect) tuples. Malformed entries
    are skipped, never raise."""
    try:
        from backend.config import get_settings
        raw = getattr(get_settings(), "uptime_http_targets", "") or ""
        targets = []
        for entry in raw.replace("\n", ",").split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = entry.split("|")
            if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
                logger.debug(f"skipping malformed uptime target: {entry!r}")
                continue
            try:
                expect = int(parts[2]) if len(parts) > 2 and parts[2].strip() else 200
            except ValueError:
                expect = 200
            targets.append((parts[0].strip(), parts[1].strip(), expect))
        return targets
    except Exception as e:
        logger.warning(f"_parse_uptime_targets failed: {e}")
        return []


async def _record_uptime():
    try:
        from sqlmodel import Session
        from backend.database import UptimeSample, engine
        from backend.integrations import (
            adguard, channels_dvr, github, hermes, homeassistant,
            obsidian, openrouter, proxmox, unifi, unraid, weather,
        )
        import time

        sources = {
            "homeassistant": homeassistant, "unifi": unifi, "unraid": unraid,
            "obsidian": obsidian, "github": github, "openrouter": openrouter,
            "weather": weather, "channels_dvr": channels_dvr, "adguard": adguard,
            "hermes": hermes, "proxmox": proxmox,
        }

        async def _check(name, mod):
            t0 = time.monotonic()
            try:
                # Bypass the shared TTL cache — the frontend polls /api/sources/status
                # every 30s and may cache a transient False for 3s; if the uptime job
                # lands in that window it records 0ms "failures" that are cache artifacts,
                # not real outages. __wrapped__ is the original undecorated function
                # (set by @wraps in cache.py) so every uptime sample is a fresh probe.
                probe = getattr(mod.health_check, "__wrapped__", mod.health_check)
                ok = await probe()
            except Exception:
                ok = False
            ms = int((time.monotonic() - t0) * 1000)
            return name, bool(ok), ms

        # Run checks SEQUENTIALLY, not concurrently. Firing all 10 at once thunders
        # the event loop with cold TLS handshakes and inflates latency with event-loop
        # queue time rather than real network time. One-at-a-time gives accurate
        # reachability + latency. 10 checks every 2 min is cheap.
        results = []
        for n, m in sources.items():
            results.append(await _check(n, m))

        # Extra plain-HTTP targets from config (GLP app, Open WebUI, etc.) —
        # sequential for the same reason as above.
        for name, url, expect in _parse_uptime_targets():
            t0 = time.monotonic()
            try:
                import httpx
                async with httpx.AsyncClient(timeout=5, verify=False) as client:
                    resp = await client.get(url)
                ok = resp.status_code == expect
            except Exception:
                ok = False
            results.append((name, ok, int((time.monotonic() - t0) * 1000)))

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
        if not result.get("online", True):
            logger.info("Speedtest skipped — no internet connectivity")
            return
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


async def _ingest_brain_spend():
    try:
        import asyncio
        from backend.agents.brain_spend import ingest_brain_spend
        await asyncio.to_thread(ingest_brain_spend)
    except Exception as e:
        logger.error(f"Brain spend ingest error: {e}")


async def _step_watchdog():
    try:
        from backend.agents.worker_pool import get_pool
        from backend.config import get_settings
        count = await get_pool().reap_hung_steps(get_settings().step_hung_timeout_s)
        if count:
            logger.info(f"Step watchdog: reaped {count} orphaned step(s)")
    except Exception as e:
        logger.error(f"Step watchdog error: {e}")


async def _propose_goals():
    try:
        from backend.agents.proposer import propose_goals_tick
        await propose_goals_tick()
    except Exception as e:
        logger.error(f"Goal proposer job error: {e}")


async def _autonomy_digest():
    try:
        from backend.agents.digest import send_autonomy_digest
        await send_autonomy_digest()
    except Exception as e:
        logger.error(f"Autonomy digest job error: {e}")


async def _backup():
    try:
        from backend.agents.backup import run_backup_job
        await run_backup_job()
    except Exception as e:
        logger.error(f"Backup job error: {e}")


async def _vault_backup():
    try:
        import asyncio
        from backend.backup import backup_vault
        result = await asyncio.to_thread(backup_vault)
        if result["ok"]:
            logger.info(f"Vault backup ok: {result['dest']}")
        else:
            logger.warning(f"Vault backup failed: {result['error']}")
            # The off-VM copy is the disaster-recovery path — a silent
            # failure here means no usable backup exists off VM 101.
            try:
                from backend import events
                await events.notify_phone(
                    f"NEXUS OFF-VM BACKUP FAILED: {result.get('error') or 'unknown error'}",
                    kind="backup_failed",
                )
            except Exception as ne:
                logger.error(f"notify_phone for vault backup failure failed: {ne}")
    except Exception as e:
        logger.error(f"Vault backup job error: {e}")


async def _checkpoint():
    try:
        from backend.agents.backup import run_checkpoint_job
        await run_checkpoint_job()
    except Exception as e:
        logger.error(f"Checkpoint job error: {e}")


async def _prune_retention():
    """Best-effort nightly prune of high-frequency sample tables. NEVER raises,
    NEVER notifies — pure background hygiene, no user-facing signal either way."""
    try:
        import asyncio
        from backend.agents.backup import prune_old_uptime_samples, prune_old_trend_snapshots, prune_old_traces
        uptime_deleted = await asyncio.to_thread(prune_old_uptime_samples)
        trend_deleted = await asyncio.to_thread(prune_old_trend_snapshots)
        trace_deleted = await asyncio.to_thread(prune_old_traces)
        logger.info(
            f"Retention prune: {uptime_deleted} uptime sample(s), {trend_deleted} trend snapshot(s), {trace_deleted} trace(s)"
        )
    except Exception as e:
        logger.error(f"Retention prune job error: {e}")


async def _watchdog():
    try:
        from backend.agents.watchdog import run_watchdog
        await run_watchdog()
    except Exception as e:
        logger.error(f"Watchdog job error: {e}")


async def _spend_report():
    try:
        from backend.agents.digest import send_spend_report
        await send_spend_report()
    except Exception as e:
        logger.error(f"Spend report job error: {e}")


async def _goal_recurrence():
    try:
        from backend.agents.goals import tick_recurring_goals
        result = await tick_recurring_goals()
        logger.info(f"Goal recurrence tick: {result}")
    except Exception as e:
        logger.error(f"Goal recurrence job error: {e}")


async def _run_brain_organizer():
    try:
        import asyncio
        import os
        import subprocess
        from pathlib import Path
        module_dir = Path(__file__).parent.parent / "modules" / "brain-organizer"
        python_exe = module_dir / "venv" / "Scripts" / "python.exe"
        script = module_dir / "brain_organizer.py"
        if not python_exe.exists() or not script.exists():
            logger.warning("Brain Organizer module not found — skipping run")
            return
        # Inherit the current environment then inject secrets from the NEXUS vault.
        # This ensures ANTHROPIC_API_KEY, OPENROUTER_API_KEY, and HERMES_HOST reach
        # the subprocess even when the parent process does not export them by default.
        env = os.environ.copy()
        try:
            from backend.config import get_settings
            s = get_settings()
            for attr, var in [
                ("anthropic_api_key", "ANTHROPIC_API_KEY"),
                ("openrouter_api_key", "OPENROUTER_API_KEY"),
                ("hermes_host", "HERMES_HOST"),
            ]:
                try:
                    val = getattr(s, attr, None)
                except Exception:
                    val = None
                if val:
                    env[var] = str(val)
        except Exception as e:
            logger.warning(f"Brain Organizer: could not inject secrets from vault ({e}) — using inherited env")
        result = await asyncio.to_thread(
            subprocess.run,
            [str(python_exe), str(script)],
            capture_output=True, text=True, cwd=str(module_dir), env=env,
        )
        if result.returncode != 0:
            logger.error(f"Brain Organizer failed (rc={result.returncode}): {result.stderr[:500]}")
        else:
            logger.info("Brain Organizer run complete")
    except Exception as e:
        logger.error(f"Brain Organizer job error: {e}")


async def _run_wiki_ingest():
    try:
        from backend.agents.wiki_ingest import run_all_unprocessed
        result = await run_all_unprocessed()
        logger.info(f"Wiki ingest batch: {result}")
    except Exception as e:
        logger.error(f"Wiki ingest job error: {e}")


async def _run_fragmentation_report():
    try:
        from backend.agents.wiki_ingest import weekly_fragmentation_report
        result = await weekly_fragmentation_report()
        logger.info(f"Wiki fragmentation report: {result}")
    except Exception as e:
        logger.error(f"Fragmentation report job error: {e}")


def setup_scheduler(briefing_time: str, timezone: str):
    hour, minute = briefing_time.split(":")
    scheduler.add_job(
        _run_briefing,
        CronTrigger(hour=int(hour), minute=int(minute), timezone=timezone),
        id="morning_briefing",
        replace_existing=True,
    )
    scheduler.add_job(
        _prune_retention,
        CronTrigger(hour=3, minute=45, timezone=timezone),
        id="retention_prune",
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
        _ingest_brain_spend,
        IntervalTrigger(seconds=300),
        id="brain_spend_ingest",
        replace_existing=True,
    )
    scheduler.add_job(
        _record_speedtest,
        # 3h, not 30m: each test saturates the link (~1.5-5.5s ping) and was
        # tripping Hermes's 5s-timeout liveness watcher into false NEXUS-down alerts.
        IntervalTrigger(hours=3),
        id="record_speedtest",
        replace_existing=True,
    )
    from backend.config import get_settings
    s = get_settings()
    if getattr(s, "step_watchdog_enabled", False):
        scheduler.add_job(
            _step_watchdog,
            IntervalTrigger(minutes=2),
            id="step_watchdog",
            replace_existing=True,
        )
        logger.info("Step watchdog enabled: runs every 2 minutes")
    if getattr(s, "proposer_enabled", False):
        scheduler.add_job(
            _propose_goals,
            IntervalTrigger(hours=max(1, getattr(s, "proposer_interval_hours", 6))),
            id="goal_proposer",
            replace_existing=True,
        )
        logger.info(f"Goal proposer enabled: every {s.proposer_interval_hours}h (suggest-only)")
    if getattr(s, "autonomy_digest_enabled", False):
        digest_time = getattr(s, "autonomy_digest_time", "20:00")
        try:
            dh, dm = digest_time.split(":")
            dh, dm = int(dh), int(dm)
        except Exception:
            logger.warning(
                f"Invalid autonomy_digest_time {digest_time!r}; falling back to 20:00"
            )
            dh, dm = 20, 0
        scheduler.add_job(
            _autonomy_digest,
            CronTrigger(hour=dh, minute=dm, timezone=timezone),
            id="autonomy_digest",
            replace_existing=True,
        )
        logger.info(f"Autonomy digest enabled: daily at {dh:02d}:{dm:02d} {timezone}")
    if getattr(s, "backup_enabled", False):
        # Hourly WAL checkpoint
        scheduler.add_job(
            _checkpoint,
            IntervalTrigger(hours=1),
            id="db_checkpoint",
            replace_existing=True,
        )
        # Daily backup at configured time
        backup_time = getattr(s, "backup_time", "03:30")
        try:
            bh, bm = backup_time.split(":")
            bh, bm = int(bh), int(bm)
        except Exception:
            logger.warning(
                f"Invalid backup_time {backup_time!r}; falling back to 03:30"
            )
            bh, bm = 3, 30
        scheduler.add_job(
            _backup,
            CronTrigger(hour=bh, minute=bm, timezone=timezone),
            id="db_backup",
            replace_existing=True,
        )
        if getattr(s, "unraid_backup_path", "").strip():
            scheduler.add_job(
                _vault_backup,
                CronTrigger(hour=bh, minute=bm + 5 if bm < 55 else 0, timezone=timezone),
                id="vault_backup",
                replace_existing=True,
            )
            logger.info(f"Vault backup to Unraid enabled: daily at {bh:02d}:{(bm+5) if bm < 55 else 0:02d} {timezone}")
        logger.info(f"Backup enabled: checkpoint hourly, backup daily at {bh:02d}:{bm:02d} {timezone}")
    if getattr(s, "watchdog_enabled", False):
        scheduler.add_job(
            _watchdog,
            IntervalTrigger(minutes=5),
            id="watchdog",
            replace_existing=True,
        )
        logger.info("Scheduler stall watchdog enabled: runs every 5 minutes")
    if getattr(s, "spend_report_enabled", False):
        report_time = getattr(s, "spend_report_time", "08:00")
        try:
            rh, rm = report_time.split(":")
            rh, rm = int(rh), int(rm)
        except Exception:
            logger.warning(
                f"Invalid spend_report_time {report_time!r}; falling back to 08:00"
            )
            rh, rm = 8, 0
        report_day = getattr(s, "spend_report_day", "mon")
        scheduler.add_job(
            _spend_report,
            CronTrigger(day_of_week=report_day, hour=rh, minute=rm, timezone=timezone),
            id="spend_report",
            replace_existing=True,
        )
        logger.info(f"Spend report enabled: weekly on {report_day} at {rh:02d}:{rm:02d} {timezone}")
    if getattr(s, "goal_recurrence_enabled", True):
        scheduler.add_job(
            _goal_recurrence,
            IntervalTrigger(minutes=30),
            id="goal_recurrence",
            replace_existing=True,
        )
        logger.info("Goal recurrence tick enabled: runs every 30 minutes")
    from pathlib import Path as _Path
    _bo_dir = _Path(__file__).parent.parent / "modules" / "brain-organizer"
    if (_bo_dir / "venv" / "Scripts" / "python.exe").exists():
        scheduler.add_job(
            _run_brain_organizer,
            CronTrigger(hour=2, minute=0, timezone=timezone),
            id="brain_organizer",
            replace_existing=True,
        )
        logger.info("Brain Organizer job registered: runs daily at 02:00 %s", timezone)
    # Daily wiki_ingest cron disabled 2026-07-14: it and brain_organizer both
    # route Brain/raw/ into wiki pages 5 minutes apart, with a known collision
    # risk over date-named pages. Brain Organizer is the sole nightly pipeline
    # now; _run_wiki_ingest/run_all_unprocessed stay unused by cron but the
    # module is still imported by wiki_fragmentation_report below.
    scheduler.add_job(
        _run_fragmentation_report,
        CronTrigger(day_of_week="sun", hour=2, minute=30, timezone=timezone),
        id="wiki_fragmentation_report",
        replace_existing=True,
    )
    logger.info("Wiki fragmentation report registered: runs Sundays at 02:30 %s", timezone)
    logger.info(f"Scheduler configured: briefing at {briefing_time} {timezone}")
