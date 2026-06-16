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
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
    }


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


def _db_create_task(prompt: str) -> int:
    """Insert a pending Task and return its id.  Called via asyncio.to_thread."""
    with Session(_db_mod.engine) as session:
        t = Task(prompt=prompt, status="pending")
        session.add(t)
        session.commit()
        session.refresh(t)
        return t.id  # type: ignore[return-value]


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
) -> dict:
    """Create a new Goal in 'proposed' status, unless debounce guards fire.

    Returns a dict with a top-level 'status' key:
      "proposed"   — goal inserted
      "debounced"  — not inserted; see 'reason' (duplicate_active | backoff | cooldown)
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

    # Mark running and record the task linkage.
    updated = await asyncio.to_thread(
        _db_update_goal, goal_id,
        status="running",
        task_id=task_id,
        updated_at=datetime.utcnow(),
    )
    return {"status": "approved", "goal": updated, "task_id": task_id}


async def reject(goal_id: int) -> dict:
    """Abandon a proposed or approved Goal (reject/cancel by a human).

    Returns 'status': not_found | conflict | abandoned.
    """
    import asyncio

    g = await asyncio.to_thread(_db_get_goal, goal_id)
    if g is None:
        return {"status": "not_found"}
    if g["status"] in ("proposed", "approved"):
        await asyncio.to_thread(
            _db_update_goal, goal_id,
            status="abandoned",
            updated_at=datetime.utcnow(),
        )
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

    running_goals = await asyncio.to_thread(_db_find_running_with_task)
    now = datetime.utcnow()
    for g in running_goals:
        try:
            task_status = await asyncio.to_thread(_db_get_task_status, g["task_id"])
            if task_status == "success":
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
