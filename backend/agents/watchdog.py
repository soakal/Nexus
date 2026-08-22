"""Scheduler stall watchdog + notification dead-letter alert (Tier 3 blind-spot removal).

Two complementary checks run on a 5-minute schedule:

1. Scheduler stall watchdog — detects individual scheduler jobs whose
   next_run_time is overdue beyond a grace window while the event loop is
   otherwise alive.  NOTE: a TOTAL loop death also kills this watchdog (that
   case is caught by health monitoring); this only catches an individual
   stalled/misfiring job.

2. Notification dead-letter alert — detects PendingDelivery rows whose attempts
   have reached or exceeded the dead_letter_attempts threshold, meaning
   Telegram has been unreachable for many consecutive retries.

Both checks are BEST-EFFORT (never raise), phone-alert via events.notify_phone,
and debounced per-condition. The dead-letter alert uses a DB-backed cooldown
(SystemState.last_dead_letter_alert_at) so the cooldown survives process
restarts — preventing a spam burst every time NEXUS reboots while the queue is
stuck. Scheduler-stall alerts use a process-local in-memory dict (acceptable
since stalls only matter while the process is running).
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# Process-local debounce state for scheduler-stall alerts only.
# Reset by reset() in tests.
_last_alert: dict[str, float] = {}


def _should_alert(key: str, cooldown_s: float, now: float | None = None) -> bool:
    """In-memory cooldown for scheduler-stall alerts.

    Returns True (and records the timestamp) if enough time has passed since
    the last alert for *key*. Passing an explicit *now* makes the logic
    deterministic in tests without sleeping.
    """
    now = now if now is not None else time.monotonic()
    last = _last_alert.get(key, 0.0)
    if now - last >= cooldown_s:
        _last_alert[key] = now
        return True
    return False


def _should_alert_dead_letters_db(cooldown_s: float) -> bool:
    """DB-backed cooldown for dead-letter alerts — survives process restarts.

    Reads SystemState.last_dead_letter_alert_at (wall-clock UTC). Returns True
    and updates the field if cooldown_s has elapsed since the last alert.
    Falls back to True on any DB error (fail-open: better to over-alert than
    silently suppress). Sync — call via asyncio.to_thread.
    """
    try:
        from sqlmodel import Session
        from backend.database import SystemState, engine

        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
        with Session(engine) as session:
            row = session.get(SystemState, 1)
            if row is None:
                return True  # no state row yet — allow alert
            last = row.last_dead_letter_alert_at
            if last is None or (now_utc - last).total_seconds() >= cooldown_s:
                row.last_dead_letter_alert_at = now_utc
                session.add(row)
                session.commit()
                return True
            return False
    except Exception as exc:
        logger.warning(f"_should_alert_dead_letters_db error (fail-open): {exc}")
        return True


# Process-local streak of consecutive integration-contract breaches, keyed by
# integration name. Reset by reset() in tests.
_contract_fail_streak: dict[str, int] = {}


def reset() -> None:
    """Clear all debounce state.  Test hook — call at the start of each test."""
    _last_alert.clear()
    _contract_fail_streak.clear()


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
                    from backend.agents import outcomes
                    d = await outcomes.record_flag_ex(
                        "watchdog", f"stall:{job.id}",
                        f"NEXUS scheduler job '{job.id}' is overdue by {int(overdue)}s"
                        " (possible stall).",
                        severity="high",
                    )
                    if d["surface"]:
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
                f"{len(rows)} Telegram deliveries dead-lettered (>= {threshold} retries) — "
                "notification pipeline likely broken (check TELEGRAM_BOT_TOKEN / Telegram reachability)"
            )
        if rows and await asyncio.to_thread(_should_alert_dead_letters_db, cooldown_s):
            from backend.agents import outcomes
            d = await outcomes.record_flag_ex(
                "watchdog", "dead_letters",
                f"NEXUS has {len(rows)} undelivered Telegram message(s) stuck"
                f" (>= {threshold} retries). Check Telegram reachability.",
                severity="high",
            )
            if d["surface"]:
                await events.notify_phone(
                    f"NEXUS has {len(rows)} undelivered Telegram message(s) stuck"
                    f" (>= {threshold} retries). Check Telegram reachability.",
                    kind="dead_letter",
                )
        return len(rows)
    except Exception as exc:
        logger.warning(f"check_dead_letters error (ignored): {exc}")
        return 0


async def check_budget_warning() -> bool:
    """Fire a single Telegram warning per local day when spend crosses
    settings.budget_warn_pct of the daily cap.

    Gated by settings.budget_warn_enabled (independent of the cap enforcement
    in governor.check_budget, which is never touched by this). Best-effort:
    any exception returns False without propagating. Returns whether it fired.
    """
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "budget_warn_enabled", True):
            return False

        pct = getattr(s, "budget_warn_pct", 0.80)
        from backend.safety import governor
        due, spend, cap = await asyncio.to_thread(governor.budget_warning_due, pct)
        if not due:
            return False

        from backend import events
        pct_used = round(spend / cap * 100) if cap > 0 else 0
        # Deliberately NOT flagged (spec docs/outcome-tracker-spec.md §2.2-B):
        # self-clearing on the next calendar-day boundary and already
        # once-per-day -- an OutcomeFlag row would add nothing here. Do not
        # "helpfully" add outcomes.record_flag to this check.
        await events.notify_phone(
            f"NEXUS spend warning: ${spend:.2f} of ${cap:.2f} daily LLM budget used "
            f"({pct_used}%). Hard cap stops billed calls at ${cap:.2f}.",
            kind="budget_warn",
        )
        return True
    except Exception as exc:
        logger.warning(f"check_budget_warning error (ignored): {exc}")
        return False


def _format_auth_burst(source: str, stat: dict, window_min: int) -> str:
    paths = ", ".join(f"{path} ({n})" for path, n in stat["paths"])
    return (
        f"NEXUS auth alert: {stat['count']} failed API-key requests (401) from "
        f"{source} in the last {window_min} min — {paths}. Likely a stale "
        f"NEXUS_API_KEY cached in a browser tab on that device; open Settings "
        f"there and re-paste the key. No further alerts for this source until "
        f"it goes quiet for {window_min} min."
    )


async def check_auth_failure_burst() -> list[str]:
    """Page once when one client floods failed API-key auths (401s).

    Gated by settings.auth_burst_enabled (independent of the 401 rejection
    itself in backend/auth.py, which this never touches). Best-effort: any
    exception returns [] without propagating. Returns the sources paged now.
    """
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "auth_burst_enabled", True):
            return []

        threshold = getattr(s, "auth_burst_threshold", 25)
        window_min = getattr(s, "auth_burst_window_minutes", 30)
        window_s = window_min * 60

        from backend.safety import authfail
        stats = authfail.recent(window_s)
        active = set(stats)
        over = {src for src, v in stats.items() if v["count"] >= threshold}

        from backend.safety import governor
        paged = await asyncio.to_thread(governor.claim_auth_burst_alert, over, active, float(window_s))

        from backend import events
        from backend.agents import outcomes
        for src in paged:
            logger.error(f"401 burst from {src}: {stats[src]['count']} failures in {window_min} min")
            d = await outcomes.record_flag_ex(
                "watchdog", f"auth_burst:{src}",
                _format_auth_burst(src, stats[src], window_min),
                severity="high",
            )
            if d["surface"]:
                await events.notify_phone(_format_auth_burst(src, stats[src], window_min), kind="auth_burst")

        return paged
    except Exception as exc:
        logger.warning(f"check_auth_failure_burst error (ignored): {exc}")
        return []


def _format_contract_breach(name: str, breaches: list[str], streak: int, window_min: int, cooldown_s: int) -> str:
    detail = "; ".join(breaches[:3])
    return (
        f"NEXUS contract alert: '{name}' returned OK but broke its expected shape "
        f"on {streak} consecutive checks ({window_min} min) — {detail}. "
        f"Downstream code will render this as fact. No further alerts for "
        f"{cooldown_s // 60} min."
    )


async def check_integration_contracts() -> list[str]:
    """Assert each integration's cached fetch() still has the shape its real
    consumers depend on (backend/safety/contracts.py). Gated by
    settings.contract_canary_enabled, independent of every other check.
    Best-effort: any exception returns [] without propagating. Returns the
    integration names paged on THIS tick.

    Deliberately reads the CACHED fetch(), never fetch.__wrapped__: the cached
    value is what briefing/tools/chat actually read, so validating it
    validates reality — and bypassing the cache would re-trigger real side
    effects (homeassistant.fetch() can POST reload_config_entry; unifi.fetch()
    writes KnownDevice rows and does a full login).

    An integration whose fetch() RAISES is not a breach — that's an outage,
    already covered by the 2-minute uptime job. Only a successful-but-wrong-
    shaped return counts, and a raise resets that integration's streak to 0.
    """
    import importlib

    from backend.safety import contracts

    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "contract_canary_enabled", True):
            return []

        consecutive_ticks = getattr(s, "contract_canary_consecutive_ticks", 3)
        cooldown_s = getattr(s, "watchdog_alert_cooldown_s", 3600)
        window_min = consecutive_ticks * 5

        paged = []
        from backend import events

        # Sequential, not asyncio.gather — same reasoning as the 2-min uptime
        # job ("firing all 10 at once thunders the event loop"). These calls
        # hit an already-warm cache, so sequential costs nothing real.
        for name, field_contracts in contracts.CONTRACTS.items():
            try:
                mod = importlib.import_module(f"backend.integrations.{name}")
                data = await mod.fetch()
            except Exception:
                _contract_fail_streak[name] = 0
                continue

            breaches = contracts.check_object(data, field_contracts)
            if not breaches:
                _contract_fail_streak[name] = 0
                continue

            _contract_fail_streak[name] = _contract_fail_streak.get(name, 0) + 1
            streak = _contract_fail_streak[name]
            if streak >= consecutive_ticks and _should_alert(f"contract:{name}", cooldown_s):
                logger.error(f"Integration contract breach: '{name}' — {'; '.join(breaches)}")
                from backend.agents import outcomes
                d = await outcomes.record_flag_ex(
                    "contracts", f"breach:{name}",
                    _format_contract_breach(name, breaches, streak, window_min, cooldown_s),
                    severity="high",
                )
                if d["surface"]:
                    await events.notify_phone(
                        _format_contract_breach(name, breaches, streak, window_min, cooldown_s),
                        kind="contract_breach",
                    )
                paged.append(name)

        return paged
    except Exception as exc:
        logger.warning(f"check_integration_contracts error (ignored): {exc}")
        return []


def _format_flag_followup(flag_id: int, flag: dict | None) -> str:
    summary = flag["summary"] if flag else f"flag #{flag_id}"
    return (
        f"NEXUS follow-up: deferred flag #{flag_id} is now due — {summary}"
    )


async def check_deferred_flags() -> list[int]:
    """Sweep deferred OutcomeFlag rows whose deferred_until has passed (rollout
    step 7, spec §3.5). Pages once per flipped id via notify_phone(kind=
    "flag_followup") with the same two-button flag:resolved/flag:false_positive
    keyboard used elsewhere (homelab_watch.py's _edge_alert).

    Gated by settings.outcome_flag_sweep_enabled, independent of
    watchdog_enabled's own gate on the caller (spec §3.5: a missed sweep is a
    late reminder, not lost data — the row persists either way).

    Best-effort: any exception returns [] without propagating. Returns the
    ids flipped this tick (does NOT mutate sweep_deferred()'s own contract —
    it still returns only ids).
    """
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "outcome_flag_sweep_enabled", True):
            return []

        from backend.agents import outcomes
        ids = await outcomes.sweep_deferred()
        if not ids:
            return ids

        # sweep_deferred() only returns ids; look up summaries for the page
        # text via the existing open_flags() reader (rows just flipped to
        # needs_follow_up are included in its result) instead of inventing a
        # new DB query pattern.
        flags = await outcomes.open_flags(limit=max(50, len(ids)))
        by_id = {f["id"]: f for f in flags}

        from backend import events
        for flag_id in ids:
            buttons = [
                {"text": "✓ Resolved", "callback_data": f"flag:resolved:{flag_id}"},
                {"text": "✗ False alarm", "callback_data": f"flag:false_positive:{flag_id}"},
            ]
            await events.notify_phone(
                _format_flag_followup(flag_id, by_id.get(flag_id)),
                kind="flag_followup",
                buttons=buttons,
            )

        return ids
    except Exception as exc:
        logger.warning(f"check_deferred_flags error (ignored): {exc}")
        return []


async def check_deploy_drift(*, cooldown_s: int) -> bool:
    """Detect a stale NEXUS process serving old code after a git pull with no
    restart. Compares the SHA captured once at boot (backend/version.py) to
    the repo's live HEAD, re-read fresh on every tick — restart-safe by
    construction, since a fresh boot always re-captures a matching SHA.

    Gated by settings.deploy_drift_check_enabled. Uses the plain in-memory
    _should_alert debounce (not the DB-backed dead-letter one — a drift
    condition only matters while this process is still running the old code).

    Escalation (opt-in, settings.deploy_drift_autorestart_enabled): tracks
    CONSECUTIVE ticks that observed drift via governor.record_deploy_drift_tick
    (persisted on SystemState.deploy_drift_streak). On the 2nd consecutive
    tick — not the first, which only alerts exactly as before — auto-restarts
    NEXUS through the broker's existing system_restart dispatch, the same
    mechanism the manual Telegram button already uses. See the call site
    below for why confirmed=True is used and how the kill switch still gates
    it.

    Best-effort: any exception returns False without propagating. Returns
    whether drift was detected on this tick (independent of whether the
    alert itself was debounced).
    """
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "deploy_drift_check_enabled", False):
            return False

        from backend import version
        running = version.running_sha()
        current = await asyncio.to_thread(version.get_git_head)

        if running is None or current is None:
            return False

        from backend.safety import governor

        if running == current:
            # No drift this tick -- reset the consecutive streak. Mirrors the
            # self-clear-on-restart behavior this check already relies on:
            # a real restart makes running==current too, so both paths reset
            # the same way.
            await asyncio.to_thread(governor.record_deploy_drift_tick, False)
            return False

        streak = await asyncio.to_thread(governor.record_deploy_drift_tick, True)

        msg = (
            f"⚠️ Deploy drift: NEXUS is running {running[:12]} but the repo is at "
            f"{current[:12]}. A git pull happened without a restart — restart NEXUS "
            "to serve current code."
        )

        if _should_alert("deploy_drift", cooldown_s):
            from backend.agents import outcomes
            from backend import events
            d = await outcomes.record_flag_ex("watchdog", "deploy_drift", msg, severity="medium")
            if d["surface"]:
                await events.notify_phone(
                    msg, kind="deploy_drift",
                    # target="lxc" (not "nexus"): the broker maps
                    # nexus/lxc/self all to THIS instance (backend/safety/
                    # broker.py), so "lxc" restarts the actually-drifted
                    # instance either way. (Historical: pre-2026-08-15
                    # shutdown, "lxc" also avoided Windows's Telegram
                    # poller consuming a "nexus" (self) target and wrongly
                    # restarting Windows -- moot now Windows is shut down.
                    # See CLAUDE.md.)
                    buttons=[{"text": "🔄 Restart NEXUS (LXC)", "callback_data": "system:restart:lxc"}],
                )

        # Auto-restart escalation -- fires on the 2nd CONSECUTIVE drift tick
        # only (streak == 2, not >=2 or first-tick): a human still gets one
        # full tick (up to 5 min) to tap the manual button above before this
        # takes over, matching today's behavior on tick 1. Using == rather
        # than >= also means a failed/refused attempt does not retry every
        # tick afterward -- the manual button remains the fallback.
        if streak == 2 and getattr(s, "deploy_drift_autorestart_enabled", False):
            from backend.safety import broker
            # broker.classify("system_restart", ...) is HIGH / REVERSIBLE_BY_
            # INVERSE (backend/safety/broker.py) -- decide() would normally
            # return NEEDS_CONFIRM for an unconfirmed autonomous actor at
            # that risk band, i.e. it would park this restart behind the
            # SAME Telegram confirm/reject tap this whole feature exists to
            # stop depending on (Brian not tapping a button is the bug being
            # fixed here -- requiring a different button would just move the
            # failure mode, not close it). confirmed=True is a deliberate,
            # narrow bypass of that one gate, scoped to this one call site --
            # not a broker policy change -- and only ever reached after the
            # 2nd-consecutive-tick + opt-in-flag gate above.
            #
            # The kill switch is NOT re-checked separately here: execute_action
            # already checks governor.get_system_state().autonomy_enabled for
            # ANY agent/autonomous actor BEFORE classify/decide ever runs,
            # unconditionally on `confirmed` (see broker.py's execute_action
            # docstring / CLAUDE.md's "Kill switch" section) -- so
            # autonomy_enabled=False still hard-FORBIDs this exact call,
            # confirmed=True included. A second pre-check here would just be
            # a driftable duplicate of that same read, not a stronger guard.
            # Every attempt -- allowed or forbidden -- is still fully audited
            # via the normal ActionLog row either way.
            await broker.execute_action(
                actor="autonomous",
                kind="system_restart",
                target="lxc",
                payload={
                    "reason": "deploy_drift_autorestart",
                    "running_sha": running,
                    "current_sha": current,
                },
                confirmed=True,
            )

        return True
    except Exception as exc:
        logger.warning(f"check_deploy_drift error (ignored): {exc}")
        return False


def _format_stale_delivery(name: str, d: dict, overdue_minutes: int) -> str:
    return (
        f"NEXUS delivery alert: '{name}' hasn't reported a heartbeat in "
        f"{overdue_minutes} min (expected every {d['expected_interval_minutes']} min "
        f"+ {d['grace_minutes']} min grace). Its pipeline may have run and produced "
        "nothing, or never ran at all — check it."
    )


async def check_expected_deliveries(*, cooldown_s: int) -> list[str]:
    """Page when a registered ExpectedDelivery (backend/agents/deliveries.py)
    goes overdue past its expected_interval_minutes + grace_minutes.

    Closes a blind spot check_scheduler_stalls can't cover: a job that FIRES
    on schedule but produces nothing, or a pipeline that lives entirely
    outside NEXUS's own scheduler — e.g. the devbox cron digest relay (cloud
    routine -> PR -> devbox cron -> vault/Telegram; see the
    "nexus-digest-health-check" skill). That relay isn't wired to send a
    heartbeat yet — it's an external cron script on a separate host, out of
    this repo's scope — POSTing to /api/deliveries/claude_digest_relay/
    heartbeat on success is a follow-up someone with access to that host
    needs to add by hand.

    Gated by settings.expected_delivery_check_enabled, independent of every
    other check. Best-effort: any exception returns [] without propagating.
    Returns the delivery names paged on THIS tick.
    """
    try:
        from backend.config import get_settings
        s = get_settings()
        if not getattr(s, "expected_delivery_check_enabled", True):
            return []

        from backend.agents import deliveries
        rows = await deliveries.list_deliveries()
        if not rows:
            return []

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        paged = []
        from backend import events
        from backend.agents import outcomes

        for d in rows:
            if not deliveries.is_overdue(d, now=now):
                continue
            last = d["last_heartbeat_at"]
            last_dt = datetime.fromisoformat(last) if last else now
            overdue_minutes = int((now - last_dt).total_seconds() / 60)

            if _should_alert(f"delivery:{d['name']}", cooldown_s):
                msg = _format_stale_delivery(d["name"], d, overdue_minutes)
                logger.error(f"Expected delivery '{d['name']}' overdue by {overdue_minutes} min")
                flag = await outcomes.record_flag_ex(
                    "watchdog", f"stale_delivery:{d['name']}", msg, severity="high",
                )
                if flag["surface"]:
                    await events.notify_phone(msg, kind="stale_delivery")
                paged.append(d["name"])

        return paged
    except Exception as exc:
        logger.warning(f"check_expected_deliveries error (ignored): {exc}")
        return []


async def run_watchdog() -> dict:
    """Top-level entry point called by the scheduler every 5 minutes.

    Gated by settings.watchdog_enabled.  Runs all eight checks and returns a
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
        budget_warn_fired = await check_budget_warning()
        auth_bursts = await check_auth_failure_burst()
        contract_breaches = await check_integration_contracts()
        deferred_swept = await check_deferred_flags()
        drift = await check_deploy_drift(cooldown_s=cooldown_s)
        stale_deliveries = await check_expected_deliveries(cooldown_s=cooldown_s)

        return {
            "stalled": stalled,
            "dead_letters": dead_count,
            "budget_warn_fired": budget_warn_fired,
            "auth_bursts": auth_bursts,
            "contract_breaches": contract_breaches,
            "deferred_swept": deferred_swept,
            "deploy_drift": drift,
            "stale_deliveries": stale_deliveries,
        }
    except Exception as exc:
        logger.error(f"run_watchdog error (ignored): {exc}")
        return {
            "stalled": [],
            "dead_letters": 0,
            "budget_warn_fired": False,
            "auth_bursts": [],
            "contract_breaches": [],
            "deferred_swept": [],
            "deploy_drift": False,
            "stale_deliveries": [],
        }
