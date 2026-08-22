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
(a blocked loop stalls every request, `/api/health` included — see CLAUDE.md).
The router's universal brake
and the orchestrator's per-task brake both wrap these in `asyncio.to_thread`.
"""

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Literal fallbacks if SystemState is missing — kept in sync with config defaults.
_DEFAULT_DAILY_BUDGET_USD = 25.0
_DEFAULT_PER_TASK_BUDGET_USD = 5.0
_DEFAULT_AUTONOMY_ENABLED = True

# Hard cap on the PERSISTED per-source claim map. Mirrors authfail.MAX_SOURCES
# for the same stated reason -- the 401 path is reachable pre-auth by anyone who
# can reach 0.0.0.0:8000 -- with the extra concern that, unlike authfail's
# in-memory table, this one survives restarts. Oldest-last-active evicted first.
_MAX_AUTH_BURST_TRACKED = 64


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
                "policy_auto_allow_kinds": set(),
                "policy_forbid_kinds": set(),
            }
        return {
            "autonomy_enabled": bool(row.autonomy_enabled),
            "daily_budget_usd": float(row.daily_budget_usd),
            "per_task_budget_usd": float(row.per_task_budget_usd),
            "policy_auto_allow_kinds": {k for k in (row.policy_auto_allow_kinds or "").split(",") if k},
            "policy_forbid_kinds": {k for k in (row.policy_forbid_kinds or "").split(",") if k},
        }


def _add_csv_kind(column: str, kind: str) -> None:
    """Shared write path for the two policy CSV columns (sync). Idempotent —
    adding an already-present kind is a no-op, not a duplicate."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)
        current = {k for k in (getattr(row, column) or "").split(",") if k}
        current.add(kind)
        setattr(row, column, ",".join(sorted(current)))
        row.updated_at = datetime.utcnow()
        session.commit()


def _remove_csv_kind(column: str, kind: str) -> None:
    """Shared write path for the two policy CSV columns (sync). Removing a
    kind that isn't present is a no-op."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            return  # nothing to remove from a row that doesn't exist yet
        current = {k for k in (getattr(row, column) or "").split(",") if k}
        current.discard(kind)
        setattr(row, column, ",".join(sorted(current)) or None)
        row.updated_at = datetime.utcnow()
        session.commit()


def get_telegram_conversation_id() -> int | None:
    """Sync. None if unset or the row doesn't exist yet — a fresh Conversation
    is created on the next chat() call either way."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        return row.telegram_conversation_id if row else None


def set_telegram_conversation_id(conversation_id: int | None) -> None:
    """Sync. Creates row 1 if missing (mirrors _add_csv_kind's pattern)."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)
        row.telegram_conversation_id = conversation_id
        row.updated_at = datetime.utcnow()
        session.commit()


def get_muted_notify_kinds() -> set[str]:
    """Sync. Runtime per-kind notify mute (Telegram /mute), distinct from the
    static Settings.phone_suppressed_kinds. Empty set if unset."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            return set()
        return {k for k in (row.muted_notify_kinds or "").split(",") if k}


# Notify kinds that must never be mutable via /mute — documented elsewhere
# (events.py, CLAUDE.md) as un-suppressible pages. A DB-hiccup fail-safe
# (events.py defaults to "not muted" on a read error) means nothing if a
# single SUCCESSFUL /mute call can silence one of these outright, with no
# TTL — indistinguishable from a harmless typo'd kind that "did nothing".
# Mirrors broker.py's _NEVER_PROMOTABLE floor on a different CSV column.
_NEVER_MUTABLE_NOTIFY_KINDS = frozenset({
    "auth_burst", "contract_breach", "budget_warn", "needs_confirm", "anthropic_credit_exhausted",
    # Same billing-problem severity class as anthropic_credit_exhausted (2026-08-21) --
    # see router._maybe_alert_provider_exhausted.
    "anthropic_usage_limit_exceeded",
})


def add_muted_notify_kind(kind: str) -> None:
    """Raises ValueError for a comma (would silently mute MULTIPLE kinds
    through the shared CSV helper — the same trap broker.py's
    _dispatch_policy_promote already guards against on a different CSV
    column; that guard didn't carry over when this column was added), a kind
    in _NEVER_MUTABLE_NOTIFY_KINDS, or a kind that is not a real notify kind.

    The unknown-kind check is LAST: the four _NEVER_MUTABLE kinds are all real
    kinds, so ordering only matters for message quality, and a comma'd string
    must still report the comma rather than "unknown kind"."""
    import difflib

    from backend.events import NOTIFY_KINDS

    if "," in kind:
        raise ValueError(f"mute kind {kind!r} must not contain a comma")
    if kind in _NEVER_MUTABLE_NOTIFY_KINDS:
        raise ValueError(f"'{kind}' is a safety-critical alert kind and cannot be muted")
    if kind not in NOTIFY_KINDS:
        near = difflib.get_close_matches(kind, sorted(NOTIFY_KINDS), n=1, cutoff=0.6)
        hint = f" Did you mean '{near[0]}'?" if near else ""
        raise ValueError(f"'{kind}' is not a notification kind NEXUS ever sends.{hint}")
    _add_csv_kind("muted_notify_kinds", kind)


def remove_muted_notify_kind(kind: str) -> bool:
    """Returns whether the kind was actually muted before this call -- so
    /unmute can say "wasn't muted" instead of falsely claiming success.
    Deliberately does NOT validate against NOTIFY_KINDS: un-muting must always
    be able to clear a stale value written before validation existed."""
    was_muted = kind in get_muted_notify_kinds()
    _remove_csv_kind("muted_notify_kinds", kind)
    return was_muted


def get_anthropic_balance_watch_state() -> dict:
    """Sync. Last-persisted signal from backend/agents/anthropic_balance_watch.py's
    monthly check (issue state/comment-count/probe-status), or all-None
    values if the row is missing or no check has run yet."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            return {"issue_signal": None, "comment_count": None, "probe_status": None}
        return {
            "issue_signal": row.anthropic_balance_watch_last_issue_signal,
            "comment_count": row.anthropic_balance_watch_last_comment_count,
            "probe_status": row.anthropic_balance_watch_last_probe_status,
        }


def set_anthropic_balance_watch_state(
    issue_signal: str | None, comment_count: int | None, probe_status: int | None
) -> None:
    """Sync. Persists the latest check's signal (whatever the caller passes
    — a failed sub-check's None-preservation is the caller's job, not
    this function's)."""
    from sqlmodel import Session

    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)
        row.anthropic_balance_watch_last_issue_signal = issue_signal
        row.anthropic_balance_watch_last_comment_count = comment_count
        row.anthropic_balance_watch_last_probe_status = probe_status
        session.commit()


def add_auto_allow_kind(kind: str) -> None:
    """Promote a kind to auto-allow for agent/autonomous actors (sync).
    Called ONLY by the broker's policy_promote dispatcher, which is itself
    gated behind a human confirm — never called directly by the learner."""
    _add_csv_kind("policy_auto_allow_kinds", kind)


def remove_auto_allow_kind(kind: str) -> None:
    """Revoke a kind's auto-allow promotion (sync). Always safe to call
    directly — revoking only removes capability, the same asymmetry as
    add_forbidden_kind below."""
    _remove_csv_kind("policy_auto_allow_kinds", kind)


def add_forbidden_kind(kind: str) -> None:
    """Demote a kind to always-forbidden for agent/autonomous actors (sync).
    Safe to call directly (no confirm gate) — tightening only removes
    capability, unlike add_auto_allow_kind."""
    _add_csv_kind("policy_forbid_kinds", kind)


def remove_forbidden_kind(kind: str) -> None:
    """Un-forbid a kind (sync)."""
    _remove_csv_kind("policy_forbid_kinds", kind)


def get_policy_overrides() -> dict:
    """Return {"auto_allow": set[str], "forbid": set[str]} (sync)."""
    state = get_system_state()
    return {
        "auto_allow": state["policy_auto_allow_kinds"],
        "forbid": state["policy_forbid_kinds"],
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


def _load_auth_burst_tracked(raw: str | None) -> dict[str, datetime]:
    """Parse SystemState.auth_burst_alert_json into {source: last_active_utc}.

    TOTAL by design -- a NULL, legacy, corrupt, or partially-malformed value
    returns {} (or drops just the bad entries) rather than raising.

    This matters more than it looks: a raise here propagates out of
    claim_auth_burst_alert into watchdog.check_auth_failure_burst's outer
    try/except, which swallows it and returns []. One poisoned column value
    would therefore silently disable the 401-burst alert FOREVER, because the
    column would never get rewritten either. Degrading to {} re-arms every
    source instead -- at worst one duplicate page, never a missed one.
    """
    if not raw:
        return {}
    out: dict[str, datetime] = {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    for src, ts in data.items():
        try:
            out[str(src)] = datetime.fromisoformat(str(ts))
        except (TypeError, ValueError):
            continue  # drop just this entry, keep the rest
    return out


def claim_auth_burst_alert(
    over_threshold: set[str], active: set[str], quiet_s: float
) -> list[str]:
    """Edge-trigger + persist the 401-burst alert state, PER SOURCE (sync).

    `over_threshold` -- sources at/above the burst threshold on this tick.
    `active`         -- sources with >=1 failure in the window on this tick
                       (always a superset of over_threshold).

    Returns the sources that crossed the edge ON THIS CALL and must be paged.
    A source already claimed is NOT returned again (page once, then go quiet),
    and the claim map survives restarts so a mid-storm reboot does not re-page.

    RE-ARM IS PER SOURCE: each claimed source carries its OWN last-active
    timestamp, and is dropped (re-arming it) once IT has produced no failure
    for `quiet_s` seconds -- regardless of what any other source is doing. The
    previous CSV + single shared timestamp meant a perpetually-storming source
    kept refreshing the one timestamp, so a different, already-quiet source
    never re-armed and its next storm was never paged.

    A source still producing failures has its timestamp refreshed every tick,
    so a storm that decays to a trickle stays claimed rather than re-paging.

    On upgrade the old auth_burst_alert_sources CSV is NOT migrated into this
    column -- a storm in flight at that exact moment gets one duplicate page,
    which is cheaper than migration code that can only ever run once.

    Creates SystemState row id=1 if it doesn't exist yet, same as
    budget_warning_due.
    """
    from sqlmodel import Session
    from backend.database import SystemState, engine

    now = datetime.utcnow()

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)
            session.commit()
            session.refresh(row)

        tracked = _load_auth_burst_tracked(row.auth_burst_alert_json)

        # 1. Re-arm per source: drop any claimed source that is producing no
        #    failures NOW and has produced none for quiet_s. A clock stepping
        #    backwards makes the delta negative -> source kept, the safe
        #    direction (no re-page).
        tracked = {
            src: last
            for src, last in tracked.items()
            if src in active or (now - last).total_seconds() < quiet_s
        }

        # 2. The edge: over threshold and not already claimed.
        new = sorted(over_threshold - set(tracked))

        # 3. Refresh every still-active claim, then claim the new ones.
        for src in list(tracked):
            if src in active:
                tracked[src] = now
        for src in new:
            tracked[src] = now

        # 4. Bound the persisted map (see _MAX_AUTH_BURST_TRACKED). Only
        #    reachable with >64 sources simultaneously over threshold; an
        #    evicted just-paged source would simply re-page next tick.
        if len(tracked) > _MAX_AUTH_BURST_TRACKED:
            tracked = dict(
                sorted(tracked.items(), key=lambda kv: kv[1], reverse=True)[
                    :_MAX_AUTH_BURST_TRACKED
                ]
            )

        row.auth_burst_alert_json = (
            json.dumps({s: t.isoformat() for s, t in sorted(tracked.items())})
            if tracked
            else None
        )
        row.updated_at = now
        session.add(row)
        session.commit()
        return new


_MAX_PROPOSER_TICK_STATS = 16  # ~4 days at the proposer's 6h cadence


def record_proposer_tick_stats(stats: dict) -> None:
    """Sync. Append one goal_proposer tick's stats to the rolling window and
    trim to the last _MAX_PROPOSER_TICK_STATS entries.

    `stats` is expected to carry proposed/auto_approved/evaluated counts and
    a `filtered` list of {"title","reason"} -- see propose_goals_tick's
    return dict. Stamps `at` (naive UTC ISO, matching every other timestamp
    convention in this module) itself so callers never need to.

    Best-effort by the caller's convention (proposer.py wraps this in its
    own try/except) -- a malformed pre-existing blob is tolerated here too,
    degrading to a fresh list rather than raising and losing this tick.
    """
    from sqlmodel import Session
    from backend.database import SystemState, engine

    entry = {
        "at": datetime.utcnow().isoformat(),
        "proposed": stats.get("count_proposed", 0),
        "auto_approved": stats.get("count_auto_approved", 0),
        "evaluated": len(stats.get("results", []) or []),
        "filtered": stats.get("filtered") or [],
    }

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)

        try:
            existing = json.loads(row.proposer_tick_stats_json or "[]")
            if not isinstance(existing, list):
                existing = []
        except (ValueError, TypeError):
            existing = []

        existing.append(entry)
        existing = existing[-_MAX_PROPOSER_TICK_STATS:]

        row.proposer_tick_stats_json = json.dumps(existing)
        row.updated_at = datetime.utcnow()
        session.commit()


def get_proposer_tick_stats(hours: int = 24) -> dict | None:
    """Sync. Aggregate proposer tick stats over the last `hours`.

    Returns None if there is no data at all (unset column, or every stored
    entry is unparseable/outside the window) so callers can render a clean
    "no ticks" line rather than a zeroed-out block. Unparseable individual
    entries are skipped, never raise -- same degrade-gracefully contract as
    _load_auth_burst_tracked above.
    """
    from sqlmodel import Session
    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        raw = row.proposer_tick_stats_json if row else None

    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(entries, list):
        return None

    cutoff = datetime.utcnow() - timedelta(hours=hours)
    ticks = proposed = auto_approved = 0
    filtered_total = 0
    filtered_by_reason: dict[str, int] = {}
    filtered_items: list[dict] = []

    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            at = datetime.fromisoformat(str(e.get("at")))
        except (TypeError, ValueError):
            continue
        if at < cutoff:
            continue
        ticks += 1
        proposed += int(e.get("proposed") or 0)
        auto_approved += int(e.get("auto_approved") or 0)
        for item in (e.get("filtered") or []):
            if not isinstance(item, dict):
                continue
            reason = str(item.get("reason") or "unknown")
            filtered_total += 1
            filtered_by_reason[reason] = filtered_by_reason.get(reason, 0) + 1
            filtered_items.append({"title": item.get("title"), "reason": reason})

    if ticks == 0:
        return None

    return {
        "ticks": ticks,
        "proposed": proposed,
        "auto_approved": auto_approved,
        "filtered_total": filtered_total,
        "filtered_by_reason": filtered_by_reason,
        # Newest first, capped -- matches the digest's other "recent items" blocks.
        "filtered_items": list(reversed(filtered_items))[:5],
    }


def record_deploy_drift_tick(drift: bool) -> int:
    """Sync. Track consecutive 5-min watchdog ticks that have observed deploy
    drift in a row, persisted on SystemState.deploy_drift_streak -- same
    singleton-row persisted-counter idiom as last_budget_warn_day/
    auth_burst_alert_json above, rather than a new mechanism.

    drift=True increments and returns the new streak; drift=False resets to
    0 (self-clears the moment a tick sees no drift, matching the boot-time
    self-clear the check itself already relies on) and returns 0. Skips the
    write entirely when the value wouldn't change (already 0, still no
    drift) -- the common case on every quiet tick. Creates SystemState row
    id=1 if it doesn't exist yet, same as budget_warning_due.
    """
    from sqlmodel import Session
    from backend.database import SystemState, engine

    with Session(engine) as session:
        row = session.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            session.add(row)
            session.commit()
            session.refresh(row)

        current = row.deploy_drift_streak or 0
        new = (current + 1) if drift else 0
        if new == current:
            return current

        row.deploy_drift_streak = new
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
        return new
