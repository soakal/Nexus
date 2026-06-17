"""Goal state-machine substrate (Tier 3 gate-blocker #3, Piece A).

propose → approved → running → completed | failed | abandoned

All sync DB helpers open their own Session and must be called ONLY via
asyncio.to_thread — no Session/ORM ever crosses an await boundary here.

get_pool is referenced at module level so tests can monkeypatch it.
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta

import backend.database as _db_mod
from sqlmodel import Session, select

from backend.database import Goal, Task

# Module-level reference so tests can monkeypatch backend.agents.goals.get_pool
from backend.agents.worker_pool import get_pool  # noqa: F401 (re-exported for patch)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category vocabulary + normalizer
# ---------------------------------------------------------------------------

GOAL_CATEGORIES = ["maintenance", "storage", "network", "media", "monitoring", "knowledge", "other"]
_CATEGORY_SET = {c for c in GOAL_CATEGORIES}


def normalize_category(category: str | None) -> str:
    """Map any input to a canonical category. Case-insensitive; unknown/None -> 'other'."""
    if not category:
        return "other"
    c = str(category).strip().lower()
    return c if c in _CATEGORY_SET else "other"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().strip())


def _fingerprint(title: str, description: str) -> str:
    """SHA-256 [:16] of normalised '{title}\\n{description}'."""
    combined = f"{_normalise(title)}\n{_normalise(description)}"
    return hashlib.sha256(combined.encode()).hexdigest()[:16]


def _goal_to_dict(g: Goal) -> dict:
    return {
        "id": g.id,
        "actor": g.actor,
        "title": g.title,
        "description": g.description,
        "status": g.status,
        "confidence": g.confidence,
        "risk": g.risk,
        "reversibility": g.reversibility,
        "fingerprint": g.fingerprint,
        "attempts": g.attempts,
        "backoff_until": g.backoff_until.isoformat() if g.backoff_until else None,
        "task_id": g.task_id,
        "proposal_at": g.proposal_at.isoformat() if g.proposal_at else None,
        "approved_by": g.approved_by,
        "approved_at": g.approved_at.isoformat() if g.approved_at else None,
        "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        "rejection_reason": g.rejection_reason,
        "cadence": g.cadence,
        "category": g.category,
        "success_criteria": g.success_criteria,
        "next_eval_at": g.next_eval_at.isoformat() if g.next_eval_at else None,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Cadence helpers (pure, no I/O)
# ---------------------------------------------------------------------------

_CADENCE_SECONDS: dict[str, int] = {
    "daily": 86400,
    "weekly": 604800,
    "monthly": 2592000,
}


def _cadence_seconds(cadence: str | None) -> int | None:
    """Return seconds for a cadence string, or None if unknown/None."""
    if cadence is None:
        return None
    return _CADENCE_SECONDS.get(cadence)


# ---------------------------------------------------------------------------
# Auto-approve policy (pure, single source of truth, no I/O)
# ---------------------------------------------------------------------------

_AUTO_APPROVE_REVERSIBLE = {"reversible", "reversible_by_inverse"}


def is_auto_approvable(goal: dict, *, enabled: bool) -> bool:
    """Default-deny policy for narrow auto-approve. True ONLY when ALL hold:
    the feature is enabled, the goal was proposed by the autonomous actor, its risk
    is 'low', and its reversibility is reversible. Everything else stays proposed for
    human approval. Irreversible/unknown reversibility and MEDIUM+ risk are NEVER
    auto-approvable, regardless of the flag."""
    if not enabled:
        return False
    return (
        str(goal.get("actor")) == "autonomous"
        and str(goal.get("risk")) == "low"
        and str(goal.get("reversibility")) in _AUTO_APPROVE_REVERSIBLE
    )


# ---------------------------------------------------------------------------
# Sync DB helpers — called exclusively via asyncio.to_thread.
# Always open a fresh Session from _db_mod.engine so monkeypatching
# backend.database.engine in tests is honoured.
# ---------------------------------------------------------------------------

_ACTIVE_STATUSES = {"proposed", "approved", "running"}


def _db_insert_goal(
    *,
    actor: str,
    title: str,
    description: str,
    status: str = "proposed",
    confidence: float = 0.6,
    risk: str = "medium",
    reversibility: str = "unknown",
    fingerprint: str = "",
    proposal_at: datetime,
    expires_at: datetime | None = None,
    cadence: str | None = None,
    category: str | None = None,
    success_criteria: str | None = None,
) -> dict:
    with Session(_db_mod.engine) as session:
        g = Goal(
            actor=actor,
            title=title,
            description=description,
            status=status,
            confidence=confidence,
            risk=risk,
            reversibility=reversibility,
            fingerprint=fingerprint,
            proposal_at=proposal_at,
            expires_at=expires_at,
            cadence=cadence,
            category=category,
            success_criteria=success_criteria,
        )
        session.add(g)
        session.commit()
        session.refresh(g)
        return _goal_to_dict(g)


def _db_get_goal(goal_id: int) -> dict | None:
    with Session(_db_mod.engine) as session:
        g = session.get(Goal, goal_id)
        return _goal_to_dict(g) if g else None


def _db_active_by_fingerprint(fp: str) -> dict | None:
    """Newest goal with this fingerprint that is in proposed|approved|running, or None."""
    with Session(_db_mod.engine) as session:
        stmt = (
            select(Goal)
            .where(Goal.fingerprint == fp)
            .where(Goal.status.in_(list(_ACTIVE_STATUSES)))  # type: ignore[attr-defined]
            .order_by(Goal.id.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        g = session.exec(stmt).first()
        return _goal_to_dict(g) if g else None


def _db_latest_by_fingerprint(fp: str) -> dict | None:
    """Newest goal with this fingerprint regardless of status, or None."""
    with Session(_db_mod.engine) as session:
        stmt = (
            select(Goal)
            .where(Goal.fingerprint == fp)
            .order_by(Goal.id.desc())  # type: ignore[attr-defined]
            .limit(1)
        )
        g = session.exec(stmt).first()
        return _goal_to_dict(g) if g else None


def _db_update_goal(goal_id: int, **fields) -> dict | None:
    with Session(_db_mod.engine) as session:
        g = session.get(Goal, goal_id)
        if g is None:
            return None
        for k, v in fields.items():
            setattr(g, k, v)
        if "updated_at" not in fields:
            g.updated_at = datetime.utcnow()
        session.add(g)
        session.commit()
        session.refresh(g)
        return _goal_to_dict(g)


def _db_list_goals(status: str | None = None, limit: int = 100) -> list[dict]:
    with Session(_db_mod.engine) as session:
        stmt = select(Goal).order_by(Goal.id.desc()).limit(limit)  # type: ignore[attr-defined]
        if status:
            stmt = stmt.where(Goal.status == status)
        goals = session.exec(stmt).all()
        return [_goal_to_dict(g) for g in goals]


def _db_recent_abandoned(limit: int = 8) -> list[dict]:
    """Return recently abandoned goals as [{title, rejection_reason}], newest first.

    Called exclusively via asyncio.to_thread.
    """
    with Session(_db_mod.engine) as session:
        stmt = (
            select(Goal)
            .where(Goal.status == "abandoned")
            .order_by(Goal.updated_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        )
        goals = session.exec(stmt).all()
        return [{"title": g.title, "rejection_reason": g.rejection_reason} for g in goals]


def _db_find_running_with_task() -> list[dict]:
    """All goals in 'running' status that have a task_id set."""
    with Session(_db_mod.engine) as session:
        stmt = (
            select(Goal)
            .where(Goal.status == "running")
            .where(Goal.task_id.isnot(None))  # type: ignore[attr-defined]
        )
        goals = session.exec(stmt).all()
        return [_goal_to_dict(g) for g in goals]


def _db_get_task_status(task_id: int) -> str | None:
    with Session(_db_mod.engine) as session:
        t = session.get(Task, task_id)
        return t.status if t else None


def _db_get_task_result(task_id: int) -> str | None:
    """Return the result_json field for a Task, or None if not found.

    Called exclusively via asyncio.to_thread — sync Session is safe here.
    """
    with Session(_db_mod.engine) as session:
        t = session.get(Task, task_id)
        return t.result_json if t else None


def _db_create_task(prompt: str) -> int:
    """Insert a pending Task and return its id.  Called via asyncio.to_thread."""
    with Session(_db_mod.engine) as session:
        t = Task(prompt=prompt, status="pending")
        session.add(t)
        session.commit()
        session.refresh(t)
        return t.id  # type: ignore[return-value]


def _db_due_recurring_goals(now: datetime) -> list[dict]:
    """Return recurring goals whose next_eval_at is due.

    A goal qualifies when ALL of:
      - cadence IS NOT NULL
      - next_eval_at IS NOT NULL
      - next_eval_at <= now
      - status IN ('completed', 'failed')

    'running' is deliberately EXCLUDED so a recurring goal NEVER overlaps itself —
    a new cycle is dispatched only after the previous cycle has FINISHED. The caller
    (tick_recurring_goals) reconciles still-running goals against their Task status
    first, so a finished-but-unreconciled goal becomes 'completed' and then eligible.
    Called exclusively via asyncio.to_thread.
    """
    with Session(_db_mod.engine) as session:
        stmt = (
            select(Goal)
            .where(Goal.cadence.isnot(None))  # type: ignore[attr-defined]
            .where(Goal.next_eval_at.isnot(None))  # type: ignore[attr-defined]
            .where(Goal.next_eval_at <= now)  # type: ignore[operator]
            .where(Goal.status.in_(["completed", "failed"]))  # type: ignore[attr-defined]
        )
        goals = session.exec(stmt).all()
        return [_goal_to_dict(g) for g in goals]


# ---------------------------------------------------------------------------
# Success-criteria evaluator (best-effort; never raises out of reconcile)
# ---------------------------------------------------------------------------

async def _evaluate_success_criteria(goal: dict, output: str | None) -> dict:
    """Ask Haiku whether the task output satisfied the goal's success_criteria.

    Returns {"met": bool, "reason": str}. On ANY failure (network, budget,
    parse error, timeout), falls back to {"met": True, "reason": "eval_unavailable"}
    so a broken evaluator NEVER fails a goal mechanically — the task's success
    stands. Haiku is a cheap, metered call; label "goal_criteria_eval".
    """
    safe_default = {"met": True, "reason": "eval_unavailable"}
    try:
        sc = goal.get("success_criteria") or ""
        title = goal.get("title") or ""

        # Truncate output to keep the prompt cheap.
        output_excerpt = (output or "")[:2000]

        prompt = (
            f"Goal title: {title}\n"
            f"Success criterion: {sc}\n\n"
            f"Task output (truncated to 2000 chars):\n{output_excerpt}\n\n"
            "Did the task output MEET the success criterion?\n"
            'Reply with ONLY valid JSON: {"met": true|false, "reason": "one sentence"}.'
        )

        from backend.agents.router import haiku
        raw = await haiku(prompt, label="goal_criteria_eval")

        # Extract the first {...} block from the response (model sometimes adds prose).
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            return safe_default
        import json
        data = json.loads(raw[start : end + 1])
        met = bool(data.get("met", True))
        reason = str(data.get("reason", ""))
        return {"met": met, "reason": reason}

    except Exception:
        # BudgetExceeded, network error, JSON parse error — all collapse to safe default.
        logger.debug("_evaluate_success_criteria failed (best-effort, ignored)", exc_info=True)
        return safe_default


# ---------------------------------------------------------------------------
# Async interface
# ---------------------------------------------------------------------------

async def propose(
    title: str,
    description: str,
    *,
    actor: str = "user",
    confidence: float = 0.6,
    risk: str = "medium",
    reversibility: str = "unknown",
    ttl_seconds: int | None = None,
    debounce_seconds: int | None = None,
    cadence: str | None = None,
    category: str | None = None,
    success_criteria: str | None = None,
) -> dict:
    """Create a new Goal in 'proposed' status, unless debounce guards fire.

    Returns a dict with a top-level 'status' key:
      "proposed"   — goal inserted
      "debounced"  — not inserted; see 'reason' (duplicate_active | backoff | cooldown)

    cadence, category, and success_criteria are optional recurring-goal fields.
    Callers that omit them get the one-shot behaviour (cadence=None).
    """
    import asyncio

    fp = _fingerprint(title, description)
    now = datetime.utcnow()

    # DEBOUNCE #1 — duplicate active
    existing = await asyncio.to_thread(_db_active_by_fingerprint, fp)
    if existing:
        return {"status": "debounced", "reason": "duplicate_active", "goal": existing}

    # DEBOUNCE #2 — backoff from a prior failure
    latest = await asyncio.to_thread(_db_latest_by_fingerprint, fp)
    if latest and latest["backoff_until"]:
        backoff_dt = datetime.fromisoformat(latest["backoff_until"])
        if now < backoff_dt:
            return {"status": "debounced", "reason": "backoff", "goal": latest}

    # DEBOUNCE #3 — cooldown since last proposal
    if debounce_seconds and latest and latest["proposal_at"]:
        last_proposal = datetime.fromisoformat(latest["proposal_at"])
        if (now - last_proposal).total_seconds() < debounce_seconds:
            return {"status": "debounced", "reason": "cooldown", "goal": latest}

    category = normalize_category(category)
    expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None
    inserted = await asyncio.to_thread(
        _db_insert_goal,
        actor=actor,
        title=title,
        description=description,
        confidence=confidence,
        risk=risk,
        reversibility=reversibility,
        fingerprint=fp,
        proposal_at=now,
        expires_at=expires_at,
        cadence=cadence,
        category=category,
        success_criteria=success_criteria,
    )
    return {"status": "proposed", "goal": inserted}


async def approve(goal_id: int, *, approved_by: str = "user") -> dict:
    """Approve a proposed Goal and dispatch a durable Task.

    This creates a normal durable Task because a HUMAN approved it — this is NOT
    autonomy.  The autonomy kill switch governs agent broker actions INSIDE the task,
    not user-approved task creation.

    Returns a dict with 'status': not_found | conflict | expired | approved.
    """
    import asyncio

    g = await asyncio.to_thread(_db_get_goal, goal_id)
    if g is None:
        return {"status": "not_found"}
    if g["status"] != "proposed":
        return {"status": "conflict", "current": g["status"]}

    now = datetime.utcnow()
    if g["expires_at"]:
        expires_dt = datetime.fromisoformat(g["expires_at"])
        if now > expires_dt:
            await asyncio.to_thread(_db_update_goal, goal_id, status="abandoned", updated_at=now)
            return {"status": "expired"}

    # Mark approved
    await asyncio.to_thread(
        _db_update_goal, goal_id,
        status="approved",
        approved_by=approved_by,
        approved_at=now,
        updated_at=now,
    )

    # DISPATCH a durable Task — a human-approved action, not autonomy.
    # Uses the module-level get_pool reference so tests can monkeypatch it.
    import backend.agents.goals as _self
    task_id = await asyncio.to_thread(_db_create_task, g["description"])
    await _self.get_pool().enqueue(task_id)

    # Build the update fields — always set status + task linkage.
    update_fields: dict = {
        "status": "running",
        "task_id": task_id,
        "updated_at": datetime.utcnow(),
    }

    # For recurring goals, schedule the FIRST recurrence now so the tick knows
    # when to re-dispatch. One-shot goals (cadence=None) never get next_eval_at.
    cadence = g.get("cadence")
    secs = _cadence_seconds(cadence)
    if secs is not None:
        update_fields["next_eval_at"] = datetime.utcnow() + timedelta(seconds=secs)

    updated = await asyncio.to_thread(_db_update_goal, goal_id, **update_fields)
    return {"status": "approved", "goal": updated, "task_id": task_id}


async def reject(goal_id: int, *, reason: str | None = None) -> dict:
    """Abandon a proposed or approved Goal (reject/cancel by a human).

    Optionally captures the human's rejection reason for proposer memory.
    Returns 'status': not_found | conflict | abandoned.
    """
    import asyncio

    g = await asyncio.to_thread(_db_get_goal, goal_id)
    if g is None:
        return {"status": "not_found"}
    if g["status"] in ("proposed", "approved"):
        update_fields: dict = {"status": "abandoned", "updated_at": datetime.utcnow()}
        if reason is not None:
            update_fields["rejection_reason"] = reason
        await asyncio.to_thread(_db_update_goal, goal_id, **update_fields)
        return {"status": "abandoned"}
    return {"status": "conflict", "current": g["status"]}


async def reconcile_running(
    *,
    backoff_base_seconds: int,
    max_attempts: int,
) -> None:
    """Sync running-goal status with their underlying Task outcomes.

    For each running Goal with a task_id:
      - task success   → goal completed
      - task failed/stopped → goal failed + attempts++ + exponential backoff_until

    Best-effort: a bad row never aborts the whole loop.
    """
    import asyncio

    from backend.config import get_settings

    running_goals = await asyncio.to_thread(_db_find_running_with_task)
    now = datetime.utcnow()
    for g in running_goals:
        try:
            task_status = await asyncio.to_thread(_db_get_task_status, g["task_id"])
            if task_status == "success":
                sc = g.get("success_criteria")
                if sc and getattr(get_settings(), "success_criteria_eval_enabled", True):
                    output = await asyncio.to_thread(_db_get_task_result, g["task_id"])
                    verdict = await _evaluate_success_criteria(g, output)
                    if verdict.get("met", True):
                        await asyncio.to_thread(
                            _db_update_goal, g["id"],
                            status="completed",
                            updated_at=now,
                        )
                    else:
                        # Criterion NOT met — treat as a failure so it retries on cadence/backoff.
                        new_attempts = g["attempts"] + 1
                        backoff_seconds = backoff_base_seconds * (2 ** min(new_attempts, 6))
                        await asyncio.to_thread(
                            _db_update_goal, g["id"],
                            status="failed",
                            attempts=new_attempts,
                            backoff_until=now + timedelta(seconds=backoff_seconds),
                            rejection_reason=f"criteria_not_met: {verdict.get('reason', '')}"[:300],
                            updated_at=now,
                        )
                else:
                    await asyncio.to_thread(
                        _db_update_goal, g["id"],
                        status="completed",
                        updated_at=now,
                    )
            elif task_status in ("failed", "stopped"):
                new_attempts = g["attempts"] + 1
                backoff_seconds = backoff_base_seconds * (2 ** min(new_attempts, 6))
                backoff_until = now + timedelta(seconds=backoff_seconds)
                await asyncio.to_thread(
                    _db_update_goal, g["id"],
                    status="failed",
                    attempts=new_attempts,
                    backoff_until=backoff_until,
                    updated_at=now,
                )
                # Best-effort phone alert for auto-approved goals that failed.
                # Inside the per-goal try/except so a notify failure never aborts the loop.
                if g.get("approved_by", "").startswith("auto:"):
                    from backend import events
                    await events.notify_phone(
                        f"NEXUS auto-started goal FAILED: {g.get('title')}",
                        kind="goal_failed",
                    )
            # still-running tasks are left untouched
        except Exception:
            logger.exception("reconcile_running: error processing goal %s", g.get("id"))


async def record_goal_result(
    goal_id: int,
    success: bool,
    *,
    backoff_base_seconds: int,
) -> dict | None:
    """Explicit result setter — for future use and tests.

    success=True  → completed
    success=False → failed + attempts++ + exponential backoff_until
    Returns updated goal dict or None if not found.
    """
    import asyncio

    g = await asyncio.to_thread(_db_get_goal, goal_id)
    if g is None:
        return None
    now = datetime.utcnow()
    if success:
        return await asyncio.to_thread(
            _db_update_goal, goal_id,
            status="completed",
            updated_at=now,
        )
    new_attempts = g["attempts"] + 1
    backoff_seconds = backoff_base_seconds * (2 ** min(new_attempts, 6))
    backoff_until = now + timedelta(seconds=backoff_seconds)
    return await asyncio.to_thread(
        _db_update_goal, goal_id,
        status="failed",
        attempts=new_attempts,
        backoff_until=backoff_until,
        updated_at=now,
    )


async def tick_recurring_goals() -> dict:
    """Scheduler tick: re-dispatch any recurring goals whose next_eval_at is due.

    Safety contract:
      - Kill-switch gated: if autonomy_enabled is False, returns immediately.
      - Best-effort: a failure on one goal never aborts the loop and never raises.
      - Re-dispatched Tasks go through the normal durable pool (no policy bypass).
      - next_eval_at is advanced to utcnow() + cadence_seconds after each dispatch.

    Returns {"redispatched": <count>} on success, or {"skipped": <reason>} when
    the kill switch is off.
    """
    import asyncio

    from backend.safety import governor

    # Kill-switch gate — all DB work via to_thread (never block the event loop).
    state = await asyncio.to_thread(governor.get_system_state)
    if not state.get("autonomy_enabled", True):
        return {"skipped": "autonomy_disabled"}

    # Reconcile first: advance any still-'running' goal whose Task has finished to
    # 'completed'/'failed' so it becomes eligible below. This is what lets a
    # recurring goal re-run WITHOUT overlapping itself (the due query excludes
    # 'running'). Best-effort — never aborts the tick.
    try:
        from backend.config import get_settings
        _s = get_settings()
        await reconcile_running(
            backoff_base_seconds=getattr(_s, "goal_backoff_base_seconds", 300),
            max_attempts=getattr(_s, "goal_max_attempts", 5),
        )
    except Exception:
        logger.exception("tick_recurring_goals: reconcile_running failed (ignored)")

    due = await asyncio.to_thread(_db_due_recurring_goals, datetime.utcnow())

    import backend.agents.goals as _self

    count = 0
    for g in due:
        try:
            task_id = await asyncio.to_thread(_db_create_task, g["description"])
            await _self.get_pool().enqueue(task_id)

            cadence = g.get("cadence")
            secs = _cadence_seconds(cadence)
            next_eval = (
                datetime.utcnow() + timedelta(seconds=secs)
                if secs is not None
                else None
            )
            update_fields: dict = {
                "status": "running",
                "task_id": task_id,
                "updated_at": datetime.utcnow(),
            }
            if next_eval is not None:
                update_fields["next_eval_at"] = next_eval

            await asyncio.to_thread(_db_update_goal, g["id"], **update_fields)
            count += 1
        except Exception:
            logger.exception(
                "tick_recurring_goals: error re-dispatching goal %s", g.get("id")
            )

    return {"redispatched": count}
