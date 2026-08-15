import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

# Brain Organizer subprocess failure log: how much of the TAIL of each stream
# to include (error is at the end of a run, not the start).
_ORGANIZER_OUTPUT_CHARS = 2000

# One-off: fires once, two days before the 2026-08-07 Infisical soak gate
# (14-day soak started 2026-07-24). Safe to delete this constant, the
# _infisical_soak_reminder job, its registration block below, and
# tests/test_infisical_soak_reminder.py once Phase 6 (Fernet vault
# retirement) ships.
INFISICAL_SOAK_REMINDER_AT = datetime(2026, 8, 5, 9, 0)
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
            adguard, calendar, channels_dvr, github, homeassistant,
            obsidian, openrouter, protonmail, proxmox, unifi, unraid, weather,
        )
        import time

        sources = {
            "homeassistant": homeassistant, "unifi": unifi, "unraid": unraid,
            "obsidian": obsidian, "github": github, "openrouter": openrouter,
            "weather": weather, "channels_dvr": channels_dvr, "adguard": adguard,
            "proxmox": proxmox, "protonmail": protonmail,
            "calendar": calendar,
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
        from backend.integrations.telegram import deliver_pending
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


async def _secret_fallback_drain():
    try:
        import asyncio
        from backend.secrets import fallback_log
        n = await asyncio.to_thread(fallback_log.drain)
        if n:
            logger.info(f"Secret fallback drain: persisted {n} key(s)")
    except Exception as e:
        logger.error(f"Secret fallback drain job error: {e}")


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


async def _run_mail_autodraft():
    try:
        from backend.agents.mail_drafts import autodraft_tick
        await autodraft_tick()
    except Exception as e:
        logger.error(f"Mail autodraft job error: {e}")


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


async def _knowledge_backup():
    try:
        import asyncio
        from backend.backup import backup_knowledge
        result = await asyncio.to_thread(backup_knowledge)
        if result["ok"]:
            logger.info(f"Knowledge backup ok: {result['dest']}")
        else:
            # Log-only, not phone-escalated like vault_backup's failure --
            # this runs every 30 min (vs once daily), a missed cycle just
            # retries next tick, not the sole disaster-recovery copy the
            # way the vault/DB backup is.
            logger.warning(f"Knowledge backup failed: {result['error']}")
    except Exception as e:
        logger.error(f"Knowledge backup job error: {e}")


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
        from backend.agents.backup import (
            prune_old_uptime_samples,
            prune_old_trend_snapshots,
            prune_old_traces,
            prune_old_outcome_flags,
        )
        uptime_deleted = await asyncio.to_thread(prune_old_uptime_samples)
        trend_deleted = await asyncio.to_thread(prune_old_trend_snapshots)
        trace_deleted = await asyncio.to_thread(prune_old_traces)
        outcome_flags_deleted = await asyncio.to_thread(prune_old_outcome_flags)
        logger.info(
            f"Retention prune: {uptime_deleted} uptime sample(s), {trend_deleted} trend snapshot(s), "
            f"{trace_deleted} trace(s), {outcome_flags_deleted} outcome flag(s)"
        )
    except Exception as e:
        logger.error(f"Retention prune job error: {e}")


async def _calibration_recompute():
    try:
        from backend.agents.calibration import recompute_hints
        result = await recompute_hints()
        logger.info(f"Calibration recompute: {result}")
    except Exception as e:
        logger.error(f"Calibration recompute job error: {e}")


async def _watchdog():
    try:
        from backend.agents.watchdog import run_watchdog
        await run_watchdog()
    except Exception as e:
        logger.error(f"Watchdog job error: {e}")


async def _homelab_watch():
    try:
        from backend.agents.homelab_watch import run_homelab_watch
        await run_homelab_watch()
    except Exception as e:
        logger.error(f"Homelab watch job error: {e}")


async def _homelab_digest():
    try:
        from backend.agents.homelab_digest import run_homelab_digest
        await run_homelab_digest()
    except Exception as e:
        logger.error(f"Homelab digest job error: {e}")


async def _spend_report():
    try:
        from backend.agents.digest import send_spend_report
        await send_spend_report()
    except Exception as e:
        logger.error(f"Spend report job error: {e}")


async def _anthropic_balance_watch():
    try:
        from backend.agents.anthropic_balance_watch import check_anthropic_balance_feature
        await check_anthropic_balance_feature()
    except Exception as e:
        logger.error(f"Anthropic balance watch job error: {e}")


async def _run_facts_digest():
    try:
        from backend.agents.facts_digest import run_facts_digest
        result = await run_facts_digest()
        logger.info(f"Facts digest job: {result}")
    except Exception as e:
        logger.error(f"Facts digest job error: {e}")


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
        python_exe = module_dir / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        script = module_dir / "brain_organizer.py"
        if not python_exe.exists() or not script.exists():
            logger.warning("Brain Organizer module not found — skipping run")
            return
        # Inherit the current environment then inject secrets from the NEXUS vault.
        # This ensures ANTHROPIC_API_KEY, OPENROUTER_API_KEY, and the Telegram
        # notify secrets reach the subprocess even when the parent process does
        # not export them by default. Sourced from the vault (not the module's
        # own .env) so the bot token never needs a second on-disk copy.
        env = os.environ.copy()
        try:
            from backend.config import get_settings
            s = get_settings()
            for attr, var in [
                ("anthropic_api_key", "ANTHROPIC_API_KEY"),
                ("openrouter_api_key", "OPENROUTER_API_KEY"),
                ("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
                ("telegram_chat_id", "TELEGRAM_CHAT_ID"),
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
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(module_dir), env=env,
        )
        if result.returncode != 0:
            # brain_organizer.py routes ALL its diagnostics through a
            # StreamHandler(sys.stdout) -- it never writes to stderr -- so
            # stdout, not stderr, is where the real failure detail lives.
            # Log the TAIL of each stream (the error is at the end of a run),
            # guarded against None (a dead/crashed child can leave a stream
            # unset even though this specific None case shouldn't occur now
            # that the encoding/errors kwargs above stop the reader thread
            # from dying mid-read).
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            stdout_tail = stdout if len(stdout) <= _ORGANIZER_OUTPUT_CHARS else "…" + stdout[-_ORGANIZER_OUTPUT_CHARS:]
            msg = f"Brain Organizer failed (rc={result.returncode}). stdout (tail): {stdout_tail}"
            if stderr:
                stderr_tail = stderr if len(stderr) <= _ORGANIZER_OUTPUT_CHARS else "…" + stderr[-_ORGANIZER_OUTPUT_CHARS:]
                msg += f" | stderr (tail): {stderr_tail}"
            msg += " See modules/brain-organizer/logs/organizer.log for full detail."
            logger.error(msg)
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


async def _infisical_soak_reminder():
    try:
        import asyncio
        import html
        from backend import events
        from backend.secrets import fallback_log

        await asyncio.to_thread(fallback_log.drain)
        summary = await asyncio.to_thread(fallback_log.summary)

        # "total_events == 0" alone can't tell "genuinely clean" apart from
        # "the DB read failed" (summary() degrades to total_events=0 plus an
        # "error" key on any exception -- fallback_log.py's own summary()
        # docstring) or "events are sitting undrained in the buffer" (the
        # "pending" field) -- for a feature built specifically because the
        # old log-based signal was unreliable, silently telling Brian it's
        # clear when the read itself failed would be the same class of bug
        # this feature exists to fix. Both degrade to the same cautious
        # message as the has-events branch, just phrased for "unknown" rather
        # than "N events".
        if summary.get("error"):
            message = (
                "NEXUS reminder: the Infisical soak gate arrives 2026-08-07 (14 "
                "days since the 2026-07-24 flip). Could not read the "
                "SecretFallback table this run — treat the fallback signal as "
                "UNKNOWN, not clean, before green-lighting Phase 6 (retiring "
                f"the Fernet vault). Error: {html.escape(str(summary['error']))}"
            )
        elif summary.get("pending", 0) > 0:
            message = (
                "NEXUS reminder: the Infisical soak gate arrives 2026-08-07 (14 "
                f"days since the 2026-07-24 flip). {summary.get('total_events', 0)} "
                "total fallback event(s) recorded so far, plus "
                f"{summary['pending']} more event(s) buffered but not yet "
                "drained to the SecretFallback table (drains every 5 min) — "
                "re-check after the next drain before treating this as final. "
                "Review before green-lighting Phase 6 (retiring the Fernet vault)."
            )
        elif summary.get("total_events", 0) == 0:
            message = (
                "NEXUS reminder: the Infisical soak gate arrives 2026-08-07 (14 "
                "days since the 2026-07-24 flip). Zero legacy-vault fallbacks "
                "have been recorded since durable tracking began (SecretFallback "
                "table, DB-backed — not a log file that truncates on every "
                "restart). Looks clear to green-light Phase 6 (retiring the "
                "Fernet vault) on that front."
            )
        else:
            top_keys = ", ".join(
                f"{html.escape(k['secret_key'])} ({k['event_count']}x, last {k['last_at']})"
                for k in summary.get("keys", [])[:3]
            )
            message = (
                "NEXUS reminder: the Infisical soak gate arrives 2026-08-07 (14 "
                f"days since the 2026-07-24 flip). {summary.get('key_count', 0)} "
                f"secret key(s) have fallen back to the legacy vault since "
                f"durable tracking began, {summary.get('total_events', 0)} total "
                f"event(s) (DB-backed via the SecretFallback table). Top keys: "
                f"{top_keys}. Review before green-lighting Phase 6 (retiring the "
                "Fernet vault)."
            )

        await events.notify_phone(message, kind="soak_reminder")
        logger.info("Infisical soak reminder sent")
    except Exception as e:
        logger.error(f"Infisical soak reminder job error: {e}")


# Pulse ticker noise control (backend/activity.py) — job completions still
# update the actor board via begin()/end() regardless, this only trims which
# jobs also write a ticker line. High-frequency housekeeping jobs would
# otherwise dominate the 200-row ring and drown out anything worth watching.
_TICKER_QUIET_JOBS = frozenset({
    "state_refresh_30s", "state_refresh_60s", "state_refresh_300s", "state_refresh_600s",
    "retry_deliveries", "secret_fallback_drain",
})

_activity_listener_registered = False


def _register_activity_listener() -> None:
    """Wire one APScheduler event listener into backend/activity.py, covering
    every registered job (present and future) with zero per-job code. Guarded
    by a module flag since setup_scheduler() runs once per test file against
    this SAME module-level `scheduler` singleton — without the guard, a full
    pytest run would stack up dozens of duplicate listeners."""
    global _activity_listener_registered
    if _activity_listener_registered:
        return
    try:
        from apscheduler.events import (
            EVENT_JOB_ERROR,
            EVENT_JOB_EXECUTED,
            EVENT_JOB_MISSED,
            EVENT_JOB_SUBMITTED,
        )
        from backend import activity

        def _on_job_event(event) -> None:
            # Sync callback (APScheduler dispatches on the loop) -- must stay
            # pure dict/deque ops via activity's own locking, never await.
            try:
                job_id = event.job_id
                actor_id = f"job:{job_id}"
                if event.code == EVENT_JOB_SUBMITTED:
                    activity.begin(actor_id, "job", job_id)
                elif event.code == EVENT_JOB_EXECUTED:
                    activity.end(actor_id, True)
                    if job_id not in _TICKER_QUIET_JOBS:
                        activity.pulse(actor_id, "job_done", f"{job_id} ok")
                elif event.code == EVENT_JOB_ERROR:
                    err = str(getattr(event, "exception", "") or "")[:200]
                    activity.end(actor_id, False, err)
                    activity.pulse(actor_id, "job_error", f"{job_id} failed: {err}")
                elif event.code == EVENT_JOB_MISSED:
                    activity.pulse(actor_id, "job_missed", f"{job_id} missed its scheduled run")
            except Exception as ex:
                logger.debug(f"activity job-event handling failed (ignored): {ex}")

        scheduler.add_listener(
            _on_job_event,
            EVENT_JOB_SUBMITTED | EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED,
        )
        _activity_listener_registered = True
    except Exception as e:
        logger.warning(f"Pulse activity listener registration failed (non-fatal): {e}")


def setup_scheduler(briefing_time: str, timezone: str):
    _register_activity_listener()
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
    # Dashboard state is refreshed by deterministic background collectors. No
    # browser request is allowed to fan out to integrations.
    from backend.state_workers import register_state_workers
    register_state_workers(scheduler)
    scheduler.add_job(
        _ingest_brain_spend,
        IntervalTrigger(seconds=300),
        id="brain_spend_ingest",
        replace_existing=True,
    )
    # Unconditional on purpose -- the durability of this audit signal must not
    # depend on watchdog_enabled or any other unrelated feature flag.
    scheduler.add_job(
        _secret_fallback_drain,
        IntervalTrigger(seconds=300),
        id="secret_fallback_drain",
        replace_existing=True,
    )
    scheduler.add_job(
        _record_speedtest,
        # 3h, not 30m: each test saturates the link (~1.5-5.5s ping), too
        # disruptive to run more often.
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
    if getattr(s, "mail_autodraft_enabled", False):
        scheduler.add_job(
            _run_mail_autodraft,
            IntervalTrigger(minutes=max(5, getattr(s, "mail_autodraft_interval_minutes", 30))),
            id="mail_autodraft",
            replace_existing=True,
        )
        logger.info(f"Mail autodraft enabled: every {s.mail_autodraft_interval_minutes}m (draft-only, never sends)")
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
            import os as _os_knowledge
            if _os_knowledge.name != "nt":
                scheduler.add_job(
                    _knowledge_backup,
                    IntervalTrigger(minutes=30),
                    id="knowledge_backup",
                    replace_existing=True,
                )
                logger.info("Knowledge store backup to Unraid enabled: every 30 min")
        logger.info(f"Backup enabled: checkpoint hourly, backup daily at {bh:02d}:{bm:02d} {timezone}")
    if getattr(s, "watchdog_enabled", False):
        scheduler.add_job(
            _watchdog,
            IntervalTrigger(minutes=5),
            id="watchdog",
            replace_existing=True,
        )
        logger.info("Scheduler stall watchdog enabled: runs every 5 minutes")
    if getattr(s, "homelab_watch_enabled", True):
        scheduler.add_job(
            _homelab_watch,
            IntervalTrigger(seconds=60),
            id="homelab_watch",
            replace_existing=True,
        )
        logger.info("Homelab watcher enabled: runs every 60 seconds")
    if getattr(s, "homelab_digest_enabled", True):
        # briefing_time + 5 minutes, wrapping hour if needed.
        total_minutes = (int(hour) * 60 + int(minute) + 5) % (24 * 60)
        dh, dm = divmod(total_minutes, 60)
        scheduler.add_job(
            _homelab_digest,
            CronTrigger(hour=dh, minute=dm, timezone=timezone),
            id="homelab_digest",
            replace_existing=True,
        )
        logger.info(f"Homelab digest enabled: daily at {dh:02d}:{dm:02d} {timezone}")
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
    if getattr(s, "anthropic_balance_watch_enabled", True):
        # 1st of each month, briefing-time-adjacent — a monthly cadence
        # comfortably outlives any transient GitHub/Anthropic API hiccup.
        scheduler.add_job(
            _anthropic_balance_watch,
            CronTrigger(day=1, hour=9, minute=30, timezone=timezone),
            id="anthropic_balance_watch",
            replace_existing=True,
        )
        logger.info("Anthropic balance-feature watch enabled: monthly on the 1st at 09:30")
    if getattr(s, "goal_recurrence_enabled", True):
        scheduler.add_job(
            _goal_recurrence,
            IntervalTrigger(minutes=30),
            id="goal_recurrence",
            replace_existing=True,
        )
        logger.info("Goal recurrence tick enabled: runs every 30 minutes")
    if getattr(s, "facts_digest_enabled", False):
        digest_time = getattr(s, "facts_digest_time", "01:30")
        try:
            fdh, fdm = digest_time.split(":")
            fdh, fdm = int(fdh), int(fdm)
        except Exception:
            logger.warning(f"Invalid facts_digest_time {digest_time!r}; falling back to 01:30")
            fdh, fdm = 1, 30
        digest_day = getattr(s, "facts_digest_day", "sun")
        # 30 min before brain_organizer's 02:00 fold (below), intentionally --
        # so this week's digest note lands in Brain/wiki/ the SAME night.
        scheduler.add_job(
            _run_facts_digest,
            CronTrigger(day_of_week=digest_day, hour=fdh, minute=fdm, timezone=timezone),
            id="facts_digest",
            replace_existing=True,
        )
        logger.info(f"Facts digest enabled: weekly on {digest_day} at {fdh:02d}:{fdm:02d} {timezone}")
    if getattr(s, "calibration_enabled", True):
        scheduler.add_job(
            _calibration_recompute,
            CronTrigger(hour=3, minute=50, timezone=timezone),
            id="calibration_recompute",
            replace_existing=True,
        )
        logger.info(f"Calibration recompute enabled: daily at 03:50 {timezone}")
    import os as _os
    from pathlib import Path as _Path
    if getattr(s, "brain_organizer_nightly_enabled", True):
        _bo_dir = _Path(__file__).parent.parent / "modules" / "brain-organizer"
        _bo_py_name = "Scripts/python.exe" if _os.name == "nt" else "bin/python"
        if (_bo_dir / "venv" / _bo_py_name).exists():
            scheduler.add_job(
                _run_brain_organizer,
                CronTrigger(hour=2, minute=0, timezone=timezone),
                id="brain_organizer",
                replace_existing=True,
            )
            logger.info("Brain Organizer job registered: runs daily at 02:00 %s", timezone)
    else:
        logger.info(
            "Brain Organizer nightly job DISABLED (brain_organizer_nightly_enabled=False) "
            "-- another instance owns nightly digestion"
        )
    # Daily wiki_ingest cron disabled 2026-07-14: it and brain_organizer both
    # route Brain/raw/ into wiki pages 5 minutes apart, with a known collision
    # risk over date-named pages. Brain Organizer is the sole nightly pipeline
    # now; _run_wiki_ingest/run_all_unprocessed stay unused by cron but the
    # module is still imported by wiki_fragmentation_report below.
    if getattr(s, "wiki_fragmentation_report_enabled", True):
        scheduler.add_job(
            _run_fragmentation_report,
            CronTrigger(day_of_week="sun", hour=2, minute=30, timezone=timezone),
            id="wiki_fragmentation_report",
            replace_existing=True,
        )
        logger.info("Wiki fragmentation report registered: runs Sundays at 02:30 %s", timezone)
    else:
        logger.info(
            "Wiki fragmentation report DISABLED (wiki_fragmentation_report_enabled=False) "
            "-- another instance owns this weekly job"
        )

    try:
        tz = ZoneInfo(timezone)
        fire_at = INFISICAL_SOAK_REMINDER_AT.replace(tzinfo=tz)
        if datetime.now(tz) < fire_at:
            scheduler.add_job(
                _infisical_soak_reminder,
                DateTrigger(run_date=INFISICAL_SOAK_REMINDER_AT, timezone=timezone),
                id="infisical_soak_reminder",
                replace_existing=True,
            )
            logger.info(
                "Infisical soak reminder registered: fires once at %s %s",
                INFISICAL_SOAK_REMINDER_AT, timezone,
            )
        else:
            logger.info("Infisical soak reminder window passed — not registering")
    except Exception as e:
        logger.warning(f"Infisical soak reminder registration skipped: {e}")

    logger.info(f"Scheduler configured: briefing at {briefing_time} {timezone}")
