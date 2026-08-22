import asyncio
import json

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlmodel import Session, select

from backend.auth import require_api_key
from backend.database import ActionLog, OutcomeFlag, TaskOutcome, get_session

router = APIRouter()


def _scheduler_running() -> bool:
    """Best-effort read of the scheduler's running flag. Guarded so the test
    fixture (which patches `scheduler` with running=False) and a not-yet-started
    scheduler both work without raising."""
    try:
        from backend.scheduler import scheduler
        return bool(getattr(scheduler, "running", False))
    except Exception:
        return False


def _parse_json(raw: str | None):
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


@router.get("/actions")
async def list_actions(
    limit: int = 50,
    decision: str | None = None,
    actor: str | None = None,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """Most-recent ActionLog rows (immutable audit trail), newest first.

    `?limit=` defaults to 50, capped at 200. Optional `?decision=` and `?actor=`
    filters. Mirrors api/tasks.py:list_tasks (pure-read GET on a Depends-injected
    Session — established NEXUS pattern, no to_thread needed here).
    """
    limit = max(1, min(limit, 200))
    stmt = select(ActionLog)
    if decision is not None:
        stmt = stmt.where(ActionLog.decision == decision)
    if actor is not None:
        stmt = stmt.where(ActionLog.actor == actor)
    stmt = stmt.order_by(ActionLog.created_at.desc()).limit(limit)
    rows = session.exec(stmt).all()

    return [
        {
            "id": r.id,
            "actor": r.actor,
            "kind": r.kind,
            "target": r.target,
            "payload": _parse_json(r.payload_json),
            "risk": r.risk,
            "reversibility": r.reversibility,
            "decision": r.decision,
            "result": _parse_json(r.result_json),
            "judge_verdict": r.judge_verdict,
            "judge_reason": r.judge_reason,
            "idempotency_key": r.idempotency_key,
            "created_at": r.created_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
            "confirmed_at": r.confirmed_at.isoformat() if r.confirmed_at else None,
        }
        for r in rows
    ]


@router.get("/outcomes")
async def list_outcomes(
    limit: int = 50,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """Recent Opus-verifier TaskOutcome rows (Tier 2.2 learning loop), newest first.

    `?limit=` defaults to 50, capped at 200. Mirrors list_actions (pure-read GET
    on a Depends-injected Session — no to_thread needed here).
    """
    limit = max(1, min(limit, 200))
    stmt = select(TaskOutcome).order_by(TaskOutcome.created_at.desc()).limit(limit)
    rows = session.exec(stmt).all()

    return [
        {
            "id": r.id,
            "task_id": r.task_id,
            "verdict": r.verdict,
            "confidence": r.confidence,
            "reason": r.reason,
            "grounded": r.grounded,
            "evidence": r.evidence,
            "model": r.model,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]



# ---------------------------------------------------------------------------
# Outcome flag tracker (docs/outcome-tracker-spec.md §3.3 — rollout step 2)
# ---------------------------------------------------------------------------
# The Claude-Code-facing close-the-loop path: create/read/resolve flags over
# REST. Writes delegate entirely to backend.agents.outcomes (dedup/suppression
# logic lives there, not here). Declared before /flags/{flag_id}/resolve so
# path matching can't shadow the literal /flags/calibration segment.

@router.get("/flags/calibration")
async def flags_calibration(days: int = 30, _=Depends(require_api_key)):
    """Per-source:check counts by status, for flags created in the last
    `?days=` (default 30). Delegates to outcomes.calibration_summary."""
    from backend.agents import outcomes

    days = max(1, min(days, 365))
    return await outcomes.calibration_summary(days)


@router.get("/flags/calibration/hints")
async def flags_calibration_hints(days: int = 30, _=Depends(require_api_key)):
    """Calibration Loop hint report (docs/calibration-loop-spec.md §4/§5.2) —
    suppressed/watching/overridden groups, in full, unpaginated (the 15-row
    watching cap is a future Telegram renderer's job, not this route's).
    A strictly longer literal segment than /flags/calibration so it can't be
    shadowed by it, but declared here (with the other calibration routes)
    and still ahead of /flags/{flag_id}/resolve per the ordering note above."""
    from backend.agents import calibration

    days = max(1, min(days, 365))
    return await calibration.hint_report(days)


@router.post("/flags/calibration/{fingerprint}/override")
async def flags_calibration_override(
    fingerprint: str,
    body: dict = Body(...),
    _=Depends(require_api_key),
):
    """Brian's explicit override (spec §5.2). Un-suppress (active=false)
    needs no gate — tightening only removes capability. Manual suppress
    (active=true) is still subject to the high-severity guardrail, enforced
    inside outcomes.should_page at read time, same as the automatic path.
    Body: {active: bool, note?: str}.
      200 — applied
      404 — no such hint (only reachable on active=false; active=true always
            creates the row)
      400 — malformed fingerprint, `active` missing/non-boolean, or `note`
            not a string / over 1000 chars
    """
    from backend.agents import calibration

    active = body.get("active")
    if not isinstance(active, bool):
        raise HTTPException(status_code=400, detail="active must be a boolean")

    note = body.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 1000):
        raise HTTPException(status_code=400, detail="note must be a string of at most 1000 characters")

    result = await calibration.set_override(
        fingerprint, active, by="api", note=note,
    )

    if result == "not_found":
        raise HTTPException(status_code=404, detail="No calibration hint for that fingerprint")
    if result == "invalid":
        raise HTTPException(status_code=400, detail="Malformed fingerprint")

    return {"fingerprint": fingerprint, "active": active, "status": result}


@router.get("/flags")
async def list_flags(
    limit: int = 50,
    status: str | None = None,
    source: str | None = None,
    _=Depends(require_api_key),
    session: Session = Depends(get_session),
):
    """Most-recent OutcomeFlag rows, newest first. `?limit=` defaults to 50,
    capped at 200. Optional `?status=` and `?source=` filters. Mirrors
    list_actions (pure-read GET on a Depends-injected Session)."""
    limit = max(1, min(limit, 200))
    stmt = select(OutcomeFlag)
    if status is not None:
        stmt = stmt.where(OutcomeFlag.status == status)
    if source is not None:
        stmt = stmt.where(OutcomeFlag.source == source)
    stmt = stmt.order_by(OutcomeFlag.id.desc()).limit(limit)
    rows = session.exec(stmt).all()

    return [
        {
            "id": r.id,
            "source": r.source,
            "check": r.check,
            "fingerprint": r.fingerprint,
            "summary": r.summary,
            "detail": r.detail,
            "severity": r.severity,
            "status": r.status,
            "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
            "resolved_by": r.resolved_by,
            "resolution_note": r.resolution_note,
            "deferred_until": r.deferred_until.isoformat() if r.deferred_until else None,
            "action_log_id": r.action_log_id,
            "surfaced_count": r.surfaced_count,
            "last_surfaced_at": r.last_surfaced_at.isoformat() if r.last_surfaced_at else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "suppressed": r.suppressed,
            "suppressed_reason": r.suppressed_reason,
        }
        for r in rows
    ]


@router.post("/flags")
async def create_flag(
    body: dict = Body(...),
    _=Depends(require_api_key),
):
    """Manual create (source="manual"), for Claude Code sessions logging their
    own observations. Delegates to outcomes.record_flag, which NEVER raises —
    a suppressed/deduped/disabled call returns id: null, not an error."""
    from backend.agents import outcomes

    check = body.get("check")
    summary = body.get("summary")
    if not check or not summary:
        raise HTTPException(status_code=400, detail="check and summary are required")

    flag_id = await outcomes.record_flag(
        "manual",
        check,
        summary,
        detail=body.get("detail"),
        severity=body.get("severity", "medium"),
    )
    return {"id": flag_id}


@router.post("/flags/{flag_id}/resolve")
async def resolve_flag_route(
    flag_id: int,
    body: dict = Body(...),
    _=Depends(require_api_key),
):
    """Close (or park) an open/needs_follow_up/deferred flag. Body:
    {status, note?, defer_days?}. Status codes mirror confirm_action:
      200  — resolved (applied status in body)
      404  — flag not found
      409  — already closed
      400  — invalid target status
    """
    from backend.agents import outcomes

    defer_days = body.get("defer_days")
    if defer_days is not None:
        if (
            isinstance(defer_days, bool)
            or not isinstance(defer_days, int)
            or not (1 <= defer_days <= 365)
        ):
            raise HTTPException(status_code=400, detail="defer_days must be a positive integer")

    result = await outcomes.resolve_flag(
        flag_id,
        body.get("status"),
        note=body.get("note"),
        by="api",
        defer_days=defer_days,
    )

    if result == "not_found":
        raise HTTPException(status_code=404, detail="Flag not found")
    if result == "already_closed":
        raise HTTPException(status_code=409, detail="Flag is already closed")
    if result == "invalid_status":
        raise HTTPException(status_code=400, detail="Invalid target status")

    return {"id": flag_id, "status": result}


@router.get("/expected-resources")
async def list_expected_resources(_=Depends(require_api_key)):
    """Declared 'what should be running' baseline (docker/vm/lxc) that
    homelab_watch.check_expected_resources diffs live state against."""
    from backend.agents import expected_resources
    return await expected_resources.list_expected()


@router.post("/expected-resources")
async def upsert_expected_resource(
    body: dict = Body(...),
    _=Depends(require_api_key),
):
    """Declare (or update) one resource's expected state. Body:
    {kind, identifier, desired_state, note?}. kind is one of docker|vm|lxc,
    desired_state is one of running|stopped."""
    from backend.agents import expected_resources

    kind = body.get("kind")
    identifier = body.get("identifier")
    desired_state = body.get("desired_state")
    if not kind or not identifier or not desired_state:
        raise HTTPException(status_code=400, detail="kind, identifier, and desired_state are required")

    try:
        row = await expected_resources.upsert(kind, identifier, desired_state, note=body.get("note"))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return row


@router.post("/expected-resources/seed")
async def seed_expected_resources(_=Depends(require_api_key)):
    """One-time (or re-runnable) baseline snapshot: declares every currently-
    observed Docker container / Proxmox VM+LXC's expected state to be
    whatever it's observed at right now -- so the feature has a real
    baseline on day one instead of being useless until manually populated.
    Safe to re-run."""
    from backend.agents import expected_resources
    return await expected_resources.seed_from_live()


@router.post("/actions/{action_id}/confirm")
async def confirm_action(
    action_id: int,
    _=Depends(require_api_key),
):
    """Confirm-and-dispatch a `needs_confirm` action (Tier 1.5 — Piece B).

    Re-checks the global kill switch and confirmation TTL at dispatch time.
    Only a row whose decision is exactly `needs_confirm` can be confirmed —
    everything else is rejected (default-deny posture). The existing ActionLog
    row is updated in place; no second row is created.

    Status codes:
      200  — dispatch attempted (status: executed | failed)
      403  — blocked by kill switch (autonomy paused)
      404  — action row not found
      409  — row exists but is not awaiting confirmation
      410  — confirmation window expired (TTL elapsed)
    """
    from backend.config import get_settings
    from backend.safety import broker

    ttl = get_settings().action_confirm_ttl_seconds
    status, res = await broker.confirm_action(action_id, ttl_seconds=ttl)

    if status == "not_found":
        raise HTTPException(status_code=404, detail="Action not found")
    if status == "not_confirmable":
        raise HTTPException(status_code=409, detail="Action is not awaiting confirmation")
    if status == "expired":
        raise HTTPException(status_code=410, detail="Confirmation window expired")
    if status == "forbidden":
        raise HTTPException(status_code=403, detail="Blocked: autonomy is paused (kill switch on)")

    # executed | failed — both return 200 with the dispatch outcome in the body
    return {
        "id": action_id,
        "status": status,
        "decision": res.decision.value if res else None,
        "result": res.result if res else None,
        "error": res.error if res else None,
    }


@router.post("/actions/{action_id}/reject")
async def reject_action(
    action_id: int,
    _=Depends(require_api_key),
):
    """Close a `needs_confirm` action without dispatching it (Telegram/web reject).

    Only a row whose decision is exactly `needs_confirm` can be rejected. No
    kill-switch or TTL check — rejection never dispatches, so it's always safe.

    Status codes:
      200  — closed (decision: forbidden, reason: rejected_by_user)
      404  — action row not found
      409  — row exists but is not awaiting confirmation
    """
    from backend.safety import broker

    status, _res = await broker.reject_action(action_id)

    if status == "not_found":
        raise HTTPException(status_code=404, detail="Action not found")
    if status == "not_confirmable":
        raise HTTPException(status_code=409, detail="Action is not awaiting confirmation")

    return {"id": action_id, "status": status}


# ---------------------------------------------------------------------------
# Cost governor / kill switch (Tier 1.5)
# ---------------------------------------------------------------------------

@router.post("/pause")
async def pause_autonomy(_=Depends(require_api_key)):
    """Global kill switch ON: disable agent/autonomous side effects + pause the
    scheduler. User actions are unaffected."""
    from backend.safety import governor
    from backend import events

    await asyncio.to_thread(governor.set_autonomy, False)
    try:
        from backend.scheduler import scheduler
        if getattr(scheduler, "running", False):
            scheduler.pause()
    except Exception:
        pass
    await events.publish("autonomy", {"enabled": False})
    return {"autonomy_enabled": False, "scheduler_running": _scheduler_running()}


@router.post("/resume")
async def resume_autonomy(_=Depends(require_api_key)):
    """Global kill switch OFF: re-enable autonomy + resume the scheduler."""
    from backend.safety import governor
    from backend import events

    await asyncio.to_thread(governor.set_autonomy, True)
    try:
        from backend.scheduler import scheduler
        if getattr(scheduler, "running", False):
            scheduler.resume()
    except Exception:
        pass
    await events.publish("autonomy", {"enabled": True})
    return {"autonomy_enabled": True, "scheduler_running": _scheduler_running()}


@router.get("/status")
async def safety_status(_=Depends(require_api_key)):
    """Current kill-switch + budget state plus today's spend and notify-channel health."""
    from backend.safety import governor
    from backend.integrations import telegram
    from backend.config import get_settings
    from backend import version

    state = await asyncio.to_thread(governor.get_system_state)
    spend = await asyncio.to_thread(governor.today_spend_usd)

    notify_channel: dict = {}
    try:
        queue_health = await asyncio.to_thread(telegram.delivery_queue_health)
        notify_channel = {
            **queue_health,
            "enabled": get_settings().phone_notifications_enabled,
        }
    except Exception:
        pass

    secret_fallback: dict = {}
    try:
        from backend.secrets import fallback_log
        secret_fallback = await asyncio.to_thread(fallback_log.summary)
    except Exception:
        pass

    return {
        "autonomy_enabled": state["autonomy_enabled"],
        "today_spend_usd": spend,
        "daily_budget_usd": state["daily_budget_usd"],
        "per_task_budget_usd": state["per_task_budget_usd"],
        "scheduler_running": _scheduler_running(),
        "notify_channel": notify_channel,
        "secret_fallback": secret_fallback,
        "running_sha": version.running_sha(),
    }


@router.delete("/deliveries/dead")
async def clear_dead_letter_deliveries(_=Depends(require_api_key)):
    """Delete all PendingDelivery rows that have exhausted retries.

    Clears stuck messages so the watchdog stops alerting and the Safety page
    delivery count resets. Also resets the DB-backed alert cooldown so the next
    genuine outage fires a fresh alert immediately.
    """
    def _purge() -> int:
        from sqlmodel import Session, select
        from backend.database import PendingDelivery, SystemState, engine
        from backend.integrations.telegram import _MAX_ATTEMPTS

        with Session(engine) as session:
            dead = session.exec(
                select(PendingDelivery).where(PendingDelivery.attempts >= _MAX_ATTEMPTS)
            ).all()
            count = len(dead)
            for row in dead:
                session.delete(row)
            # Also reset the alert cooldown so the next real outage fires fresh.
            state = session.get(SystemState, 1)
            if state:
                state.last_dead_letter_alert_at = None
                session.add(state)
            session.commit()
            return count

    cleared = await asyncio.to_thread(_purge)
    return {"cleared": cleared}


@router.get("/metering")
async def metering_health(_=Depends(require_api_key)):
    """Live spend-metering health: process-lifetime outcome counters + today's
    spend and row count + whether prices have been field-verified."""
    from backend.safety import governor

    return await asyncio.to_thread(governor.metering_health)


@router.get("/spend-report")
async def spend_report(days: int = 7, _=Depends(require_api_key)):
    """Per-model + per-label spend breakdown over the last ?days= (default 7)."""
    from backend.safety import governor

    days = max(1, min(days, 90))
    return await asyncio.to_thread(governor.spend_report, days)



@router.post("/budget")
async def set_budget(
    body: dict = Body(default_factory=dict),
    _=Depends(require_api_key),
):
    """Runtime cap-setter. Body: {daily_usd?: float, per_task_usd?: float}.
    Returns the new state."""
    from backend.safety import governor

    daily = body.get("daily_usd")
    per_task = body.get("per_task_usd")
    await asyncio.to_thread(
        governor.set_budgets,
        float(daily) if daily is not None else None,
        float(per_task) if per_task is not None else None,
    )
    state = await asyncio.to_thread(governor.get_system_state)
    spend = await asyncio.to_thread(governor.today_spend_usd)
    return {
        "autonomy_enabled": state["autonomy_enabled"],
        "today_spend_usd": spend,
        "daily_budget_usd": state["daily_budget_usd"],
        "per_task_budget_usd": state["per_task_budget_usd"],
        "scheduler_running": _scheduler_running(),
    }


# ---------------------------------------------------------------------------
# Confirm-policy overrides (Feature 3 — Confirm-Policy Learner, Phase 1)
# ---------------------------------------------------------------------------
# Reading and revoking are plain GET/DELETE — granting a promotion is
# deliberately NOT here: that only ever happens via the broker's
# policy_promote kind + the existing safety:confirm Telegram buttons, so a
# promotion always goes through a real human confirm. Revoking a promotion
# (the DELETE below) needs no such gate — it only removes capability.

@router.get("/policy")
async def get_policy(_=Depends(require_api_key)):
    """Current confirm-policy overrides: kinds promoted to auto-allow and
    kinds demoted to always-forbidden for agent/autonomous actors."""
    from backend.safety import governor

    overrides = await asyncio.to_thread(governor.get_policy_overrides)
    return {
        "auto_allow": sorted(overrides["auto_allow"]),
        "forbid": sorted(overrides["forbid"]),
    }


@router.delete("/policy/forbid/{kind}")
async def delete_forbidden_kind(kind: str, _=Depends(require_api_key)):
    """Remove a kind from the always-forbidden list (un-demote it)."""
    from backend.safety import governor

    await asyncio.to_thread(governor.remove_forbidden_kind, kind)
    overrides = await asyncio.to_thread(governor.get_policy_overrides)
    return {"ok": True, "forbid": sorted(overrides["forbid"])}


@router.delete("/policy/auto-allow/{kind}")
async def delete_auto_allow_kind(kind: str, _=Depends(require_api_key)):
    """Revoke a kind's auto-allow promotion. Always allowed without a confirm
    gate — unlike granting one, revoking only removes capability."""
    from backend.safety import governor

    await asyncio.to_thread(governor.remove_auto_allow_kind, kind)
    overrides = await asyncio.to_thread(governor.get_policy_overrides)
    return {"ok": True, "auto_allow": sorted(overrides["auto_allow"])}
