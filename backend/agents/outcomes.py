"""Outcome flag tracker (docs/outcome-tracker-spec.md) — the write/dedup/read
foundation, now wired end-to-end (rollout steps 1-7 complete; see this repo's
own CLAUDE.md "Outcome Tracker" section for the full call-site map):
homelab_watch.py, watchdog.py, briefing.py, telegram_poller.py/
telegram_commands.py, tools.py (read-only), and backup.py all call into this
module. ActionLog itself remains untouched (no new columns/decision values —
see spec §5.8) — this module writes only to OutcomeFlag.

Follows the established shape (digest.py, goals.py, facts.py): sync `_db_*`
helpers open their own Session (re-importing `engine` from backend.database
on every call so tests that monkeypatch backend.database.engine are
honoured), and the public async functions call them exclusively via
asyncio.to_thread. No Session/ORM object ever crosses an await boundary —
`_db_*` helpers return plain dicts, never SQLModel instances (AC33).

No import of backend.safety.broker or anything from the safety broker/
websocket layer (AC32) — a deliberate invariant of this module, not a
step-1-only artifact. Zero LLM calls anywhere in this module (AC34).

Explicitly out of v1 scope (see spec §5): no LLM classification of outcomes,
no auto-suppression by false-positive rate beyond the fixed cooldown below,
no parsing of the briefing's "## Priority Actions" LLM prose, no agent-facing
write tool (a resolve_flag write tool would let NEXUS close its own loop and
destroy the signal the feature exists to capture), no frontend page, no
Obsidian/Vault write path.
"""
import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# The five values OutcomeFlag.status may ever hold (backend/database.py).
_VALID_TARGET_STATUSES = {"open", "resolved", "deferred", "false_positive", "needs_follow_up"}

# "Still live" statuses — record_flag's dedup branch 1 treats either as
# "already raised, don't duplicate"; clear_flag auto-resolves either.
_ACTIVE_STATUSES = {"open", "needs_follow_up"}

# Terminal statuses — resolve_flag refuses to re-resolve these (AC11's
# "already_closed"). "deferred" and "needs_follow_up" are deliberately NOT
# here: both are still-open items a human/auto action can resolve early.
_CLOSED_STATUSES = {"resolved", "false_positive"}


def _flag_to_dict(f) -> dict:
    return {
        "id": f.id,
        "source": f.source,
        "check": f.check,
        "fingerprint": f.fingerprint,
        "summary": f.summary,
        "detail": f.detail,
        "severity": f.severity,
        "status": f.status,
        "resolved_at": f.resolved_at.isoformat() if f.resolved_at else None,
        "resolved_by": f.resolved_by,
        "resolution_note": f.resolution_note,
        "deferred_until": f.deferred_until.isoformat() if f.deferred_until else None,
        "action_log_id": f.action_log_id,
        "surfaced_count": f.surfaced_count,
        "last_surfaced_at": f.last_surfaced_at.isoformat() if f.last_surfaced_at else None,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Sync DB helpers — called exclusively via asyncio.to_thread. Always open a
# fresh Session from a freshly-imported backend.database.engine so
# monkeypatching backend.database.engine in tests is honoured (matches
# digest.py / goals.py).
# ---------------------------------------------------------------------------

def _db_find_active_by_fingerprint(fingerprint: str) -> dict | None:
    """Newest row with this fingerprint in status open|needs_follow_up, or None."""
    from sqlmodel import Session, select
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        stmt = (
            select(OutcomeFlag)
            .where(OutcomeFlag.fingerprint == fingerprint)
            .where(OutcomeFlag.status.in_(list(_ACTIVE_STATUSES)))  # type: ignore[attr-defined]
            .order_by(OutcomeFlag.id.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        row = session.exec(stmt).first()
        return _flag_to_dict(row) if row else None


def _db_latest_by_fingerprint(fingerprint: str) -> dict | None:
    """Newest row with this fingerprint regardless of status, or None."""
    from sqlmodel import Session, select
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        stmt = (
            select(OutcomeFlag)
            .where(OutcomeFlag.fingerprint == fingerprint)
            .order_by(OutcomeFlag.id.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        row = session.exec(stmt).first()
        return _flag_to_dict(row) if row else None


def _db_bump_surfaced(flag_id: int) -> dict | None:
    """Increment surfaced_count and bump last_surfaced_at on an already-open row."""
    from sqlmodel import Session
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        row = session.get(OutcomeFlag, flag_id)
        if row is None:
            return None
        now = datetime.utcnow()
        row.surfaced_count += 1
        row.last_surfaced_at = now
        row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _flag_to_dict(row)


def _db_insert_flag(
    *,
    source: str,
    check: str,
    fingerprint: str,
    summary: str,
    detail: str | None,
    severity: str,
    action_log_id: int | None,
) -> dict | None:
    """Returns None only when ux_outcomeflag_open lost a concurrent insert race —
    see the IntegrityError handling below, same pattern as goals._db_insert_goal
    against ux_goal_fingerprint_active."""
    from sqlalchemy.exc import IntegrityError
    from sqlmodel import Session
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        row = OutcomeFlag(
            source=source,
            check=check,
            fingerprint=fingerprint,
            summary=summary,
            detail=detail,
            severity=severity,
            action_log_id=action_log_id,
        )
        session.add(row)
        try:
            session.commit()
        except IntegrityError:
            # ux_outcomeflag_open lost the race — a concurrent record_flag()
            # call for the same fingerprint committed first. Not an error: the
            # caller re-fetches the winner and returns its id.
            session.rollback()
            return None
        session.refresh(row)
        return _flag_to_dict(row)


def _db_get_flag(flag_id: int) -> dict | None:
    from sqlmodel import Session
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        row = session.get(OutcomeFlag, flag_id)
        return _flag_to_dict(row) if row else None


def _db_update_flag(flag_id: int, **fields) -> dict | None:
    from sqlmodel import Session
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        row = session.get(OutcomeFlag, flag_id)
        if row is None:
            return None
        for k, v in fields.items():
            setattr(row, k, v)
        if "updated_at" not in fields:
            row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _flag_to_dict(row)


def _db_clear_by_fingerprint(fingerprint: str, by: str) -> int:
    """Auto-resolve every still-active (open|needs_follow_up) row for this
    fingerprint. Returns the count of rows resolved."""
    from sqlmodel import Session, select
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        stmt = (
            select(OutcomeFlag)
            .where(OutcomeFlag.fingerprint == fingerprint)
            .where(OutcomeFlag.status.in_(list(_ACTIVE_STATUSES)))  # type: ignore[attr-defined]
        )
        rows = session.exec(stmt).all()
        now = datetime.utcnow()
        count = 0
        for row in rows:
            row.status = "resolved"
            row.resolved_at = now
            row.resolved_by = by
            row.updated_at = now
            session.add(row)
            count += 1
        if count:
            session.commit()
        return count


def _db_open_flags(limit: int, include_suppressed: bool = False) -> list[dict]:
    """Open + needs_follow_up + deferred-past-due, newest first."""
    from sqlmodel import Session, select
    from backend.database import OutcomeFlag, engine

    now = datetime.utcnow()
    with Session(engine) as session:
        stmt = select(OutcomeFlag).order_by(OutcomeFlag.id.desc())  # type: ignore[attr-defined]
        rows = session.exec(stmt).all()
        out = [
            row for row in rows
            if (row.status in _ACTIVE_STATUSES
                or (row.status == "deferred" and row.deferred_until is not None and row.deferred_until <= now))
            and (include_suppressed or not row.suppressed)
        ]
        return [_flag_to_dict(row) for row in out[:limit]]


def _db_recently_closed(cutoff: datetime, limit: int) -> list[dict]:
    """Rows resolved/false_positive'd since cutoff, newest first."""
    from sqlmodel import Session, select
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        stmt = (
            select(OutcomeFlag)
            .where(OutcomeFlag.status.in_(list(_CLOSED_STATUSES)))  # type: ignore[attr-defined]
            .where(OutcomeFlag.resolved_at.is_not(None))  # type: ignore[attr-defined]
            .where(OutcomeFlag.resolved_at >= cutoff)  # type: ignore[attr-defined]
            .order_by(OutcomeFlag.resolved_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        rows = session.exec(stmt).all()
        return [_flag_to_dict(row) for row in rows]


def _db_calibration(cutoff: datetime) -> dict:
    """Per-source:check counts by status, for rows created since cutoff."""
    from sqlmodel import Session, select
    from backend.database import OutcomeFlag, engine

    with Session(engine) as session:
        stmt = select(OutcomeFlag).where(OutcomeFlag.created_at >= cutoff)  # type: ignore[attr-defined]
        rows = session.exec(stmt).all()
        result: dict = {}
        for row in rows:
            key = f"{row.source}:{row.check}"
            bucket = result.setdefault(key, {})
            bucket[row.status] = bucket.get(row.status, 0) + 1
        return result


def _db_active_hint(fingerprint: str) -> dict | None:
    """The current ACTIVE calibration hint for this fingerprint, or None.
    Returns a plain dict (never an ORM object — no Session may cross an
    await boundary, AC33/CAL48's discipline). A hint whose status isn't
    "active", or whose override_until hasn't lapsed yet (defensive — the
    nightly recompute job, backend/agents/calibration.py, is the primary
    owner of that check, but should_page re-checks it directly so a stale
    row can never gate a page), is treated as "no active hint" so callers
    need not special-case it. Any DB error (missing table, etc.) propagates
    to the caller, which fails open (spec §3.2/§3.4)."""
    from sqlmodel import Session, select
    from backend.database import CalibrationHint, engine

    with Session(engine) as session:
        stmt = select(CalibrationHint).where(CalibrationHint.fingerprint == fingerprint)
        row = session.exec(stmt).first()
        if row is None or row.status != "active":
            return None
        if row.override_until is not None and row.override_until > datetime.utcnow():
            return None
        return {
            "id": row.id,
            "fingerprint": row.fingerprint,
            "status": row.status,
            "verdict_count": row.verdict_count,
            "false_positive_count": row.false_positive_count,
            "fp_rate": row.fp_rate,
            "max_severity": row.max_severity,
            "reason": row.reason,
        }


def _db_sweep_deferred() -> list[int]:
    """Flip deferred rows whose deferred_until has passed to needs_follow_up.
    Returns the ids flipped."""
    from sqlmodel import Session, select
    from backend.database import OutcomeFlag, engine

    now = datetime.utcnow()
    with Session(engine) as session:
        stmt = (
            select(OutcomeFlag)
            .where(OutcomeFlag.status == "deferred")  # type: ignore[attr-defined]
            .where(OutcomeFlag.deferred_until.is_not(None))  # type: ignore[attr-defined]
            .where(OutcomeFlag.deferred_until <= now)  # type: ignore[attr-defined]
        )
        rows = session.exec(stmt).all()
        ids: list[int] = []
        for row in rows:
            row.status = "needs_follow_up"
            row.updated_at = now
            session.add(row)
            ids.append(row.id)
        if ids:
            session.commit()
        return ids


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def active_hint(fingerprint: str) -> dict | None:
    """Hot-path read (spec §3.2): the current ACTIVE calibration hint for
    `fingerprint`, or None (no hint, hint not active/overridden/expired, an
    override still in effect, a missing table, or any error). NEVER raises —
    same contract as record_flag."""
    try:
        return await asyncio.to_thread(_db_active_hint, fingerprint)
    except Exception as e:
        logger.warning(f"active_hint failed (fingerprint={fingerprint!r}): {e}")
        return None


async def should_page(source: str, check: str, severity: str = "medium") -> tuple[bool, str | None]:
    """The suppression gate (spec §3.2). Returns (True, None) to page, or
    (False, reason) to stay quiet.

    Fails open on absolutely everything: a manual source, calibration_enabled
    =False, calibration_suppression_enabled=False, no active hint row, a
    severity the high-severity guardrail protects (severity=="high" unless
    calibration_suppress_high_severity is explicitly True), or any exception
    -> (True, None). NEVER raises — wrapped in one outer try/except exactly
    like record_flag."""
    try:
        if source == "manual":
            # A human-entered flag (telegram /flag, POST /flags) is never
            # auto-suppressed (spec §3.3) — checked before any hint lookup.
            return True, None

        from backend.config import get_settings
        settings = get_settings()
        if not getattr(settings, "calibration_enabled", True):
            return True, None
        if not getattr(settings, "calibration_suppression_enabled", False):
            return True, None
        if severity == "high" and not getattr(settings, "calibration_suppress_high_severity", False):
            return True, None

        fingerprint = f"{source}:{check}"
        hint = await active_hint(fingerprint)
        if hint is None:
            return True, None

        reason = hint["reason"] or (
            f"calibration:{fingerprint} fp_rate={hint['fp_rate']:.2f} n={hint['verdict_count']}"
        )
        return False, reason
    except Exception as e:
        logger.warning(f"should_page failed (source={source!r}, check={check!r}): {e}")
        return True, None


async def record_flag_ex(
    source: str,
    check: str,
    summary: str,
    *,
    detail: str | None = None,
    severity: str = "medium",
    action_log_id: int | None = None,
) -> dict:
    """The richer write API (spec §3.2) `record_flag` now wraps. Returns
    {"id": int | None, "surface": bool, "reason": str | None}.

    NEVER raises — same contract as record_flag. Runs record_flag's original
    four-branch write/dedup logic unchanged, plus the write/page split (spec
    §3.1): the row is always written (or bumped), but whether the caller
    should actually page the human is decided separately by `should_page` and
    returned via `surface`/`reason`:
      0. Consult should_page() up front. A "no" doesn't stop the write below
         — it stamps suppressed=True/suppressed_reason on whichever row gets
         written or bumped, and the return says not to surface it.
      1. Existing open|needs_follow_up row -> bump surfaced_count, return its
         id with surface per the gate (never surface=False merely because
         the row already existed — the callers' own latches already own
         re-alert suppression, spec §3.2).
      2. Existing deferred row still within its window -> id=None,
         surface=False, reason="deferred" (Finding B's page-through fix).
      3. Existing false_positive row within the cooldown -> id=None,
         surface=False, reason="false_positive_cooldown" (ditto).
      4. Otherwise INSERT a new open row (IntegrityError from the partial
         unique index -> re-SELECT the winner), stamped per the gate.
    """
    try:
        from backend.config import get_settings
        settings = get_settings()
        if not getattr(settings, "outcome_flags_enabled", True):
            # Fail open (same contract as should_page): with the tracker off,
            # paging must behave exactly as it did before record_flag_ex
            # existed -- id stays None so record_flag's back-compat wrapper
            # still returns None, but surface=True so callers still page.
            return {"id": None, "surface": True, "reason": "outcome_flags_disabled"}

        fingerprint = f"{source}:{check}"
        now = datetime.utcnow()
        page, page_reason = await should_page(source, check, severity)

        # Branch 1 — already raised and still open/needs_follow_up.
        active = await asyncio.to_thread(_db_find_active_by_fingerprint, fingerprint)
        if active:
            bumped = await asyncio.to_thread(_db_bump_surfaced, active["id"])
            flag_id = bumped["id"] if bumped else active["id"]
            return {"id": flag_id, "surface": page, "reason": None if page else page_reason}

        latest = await asyncio.to_thread(_db_latest_by_fingerprint, fingerprint)
        if latest:
            # Branch 2 — deferred, still within the "shut up until" window.
            if latest["status"] == "deferred" and latest["deferred_until"]:
                deferred_until = datetime.fromisoformat(latest["deferred_until"])
                if now < deferred_until:
                    return {"id": None, "surface": False, "reason": "deferred"}
            # Branch 3 — false_positive within the cooldown.
            if latest["status"] == "false_positive" and latest["resolved_at"]:
                resolved_at = datetime.fromisoformat(latest["resolved_at"])
                cooldown_days = int(getattr(settings, "outcome_flag_false_positive_cooldown_days", 30))
                if now - resolved_at < timedelta(days=cooldown_days):
                    return {"id": None, "surface": False, "reason": "false_positive_cooldown"}

        # Branch 4 — otherwise, insert a new open row.
        inserted = await asyncio.to_thread(
            _db_insert_flag,
            source=source,
            check=check,
            fingerprint=fingerprint,
            summary=summary,
            detail=detail,
            severity=severity,
            action_log_id=action_log_id,
        )
        if inserted is None:
            winner = await asyncio.to_thread(_db_find_active_by_fingerprint, fingerprint)
            flag_id = winner["id"] if winner else None
        else:
            flag_id = inserted["id"]

        if flag_id is not None and not page:
            await asyncio.to_thread(
                _db_update_flag, flag_id, suppressed=True, suppressed_reason=page_reason,
            )
        return {"id": flag_id, "surface": page, "reason": None if page else page_reason}
    except Exception as e:
        logger.warning(f"record_flag_ex failed (source={source!r}, check={check!r}): {e}")
        return {"id": None, "surface": True, "reason": None}


async def record_flag(
    source: str,
    check: str,
    summary: str,
    *,
    detail: str | None = None,
    severity: str = "medium",
    action_log_id: int | None = None,
) -> int | None:
    """Record (or re-surface) an observation. Returns the row id (new or the
    existing open row's), or None if suppressed/disabled/errored.

    NEVER raises — same contract as events.notify_phone. Now a thin
    back-compat wrapper over record_flag_ex (spec §3.2): the four-branch
    write/dedup semantics below are unchanged, byte-identical, for every
    existing call site — this signature and its return values are locked by
    tests/test_outcome_flags.py (CAL51).
      1. Existing open|needs_follow_up row for this fingerprint -> bump
         surfaced_count/last_surfaced_at, return its id. No new row.
      2. Existing deferred row with deferred_until still in the future ->
         return None ("deferred means shut up until then").
      3. Existing false_positive row resolved within the cooldown window ->
         return None ("stop crying wolf on the same pattern").
      4. Otherwise INSERT a new open row (IntegrityError from the partial
         unique index -> re-SELECT the winner and return its id).
    """
    d = await record_flag_ex(
        source, check, summary,
        detail=detail, severity=severity, action_log_id=action_log_id,
    )
    return d["id"] if d["surface"] or d["id"] else None


async def resolve_flag(
    flag_id: int,
    status: str,
    *,
    note: str | None = None,
    by: str = "api",
    defer_days: int | None = None,
) -> str:
    """Close (or park) an open/needs_follow_up/deferred flag.

    Returns "not_found" | "already_closed" | "invalid_status" | the applied
    status string on success (e.g. "resolved", "deferred", "false_positive",
    "needs_follow_up") — mirrors broker.reject_action's status-string return
    convention so Telegram/REST/tests share one mapping.
    """
    if status not in _VALID_TARGET_STATUSES:
        return "invalid_status"

    row = await asyncio.to_thread(_db_get_flag, flag_id)
    if row is None:
        return "not_found"
    if row["status"] in _CLOSED_STATUSES:
        return "already_closed"

    now = datetime.utcnow()
    update_fields: dict = {
        "status": status,
        "resolved_at": now,
        "resolved_by": by,
        "resolution_note": note,
        "updated_at": now,
    }
    if status == "deferred" and defer_days is not None:
        update_fields["deferred_until"] = now + timedelta(days=defer_days)

    updated = await asyncio.to_thread(_db_update_flag, flag_id, **update_fields)
    if updated is None:
        return "not_found"
    return status


async def clear_flag(source: str, check: str, *, by: str = "auto:condition_cleared") -> int:
    """Auto-resolve any open/needs_follow_up flag matching this fingerprint.
    Returns the count resolved (0 if none were open)."""
    fingerprint = f"{source}:{check}"
    return await asyncio.to_thread(_db_clear_by_fingerprint, fingerprint, by)


async def open_flags(limit: int = 50, *, include_suppressed: bool = False) -> list[dict]:
    """Open + needs_follow_up + deferred-past-due, newest first."""
    return await asyncio.to_thread(_db_open_flags, limit, include_suppressed)


async def recently_closed(hours: int = 48, limit: int = 30) -> list[dict]:
    """Resolved/false_positive'd within the last `hours`, newest first — for
    the briefing's "do not re-raise" block."""
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return await asyncio.to_thread(_db_recently_closed, cutoff, limit)


async def calibration_summary(days: int = 30) -> dict:
    """Per-source:check counts by status, for flags created in the last `days`.
    Returns {} on an empty table (or no rows in the window)."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    return await asyncio.to_thread(_db_calibration, cutoff)


async def sweep_deferred() -> list[int]:
    """Flip deferred rows whose deferred_until has passed to needs_follow_up.
    Returns the ids flipped (called from watchdog.run_watchdog(), rollout step 7)."""
    return await asyncio.to_thread(_db_sweep_deferred)
