"""Action Judge — a safety/context "should this happen at all?" gate.

This is NOT the same check as the orchestrator's post-hoc verifier
(`backend/agents/orchestrator.py::_opus_verify` — not touched by this module):

  * The JUDGE (this module, `evaluate_action`) fires BEFORE a side-effecting
    action dispatches. It is a safety/context judgment made with everything
    relevant to the moment: what the action actually is, what task proposed
    it, whether it's day or night, whether an existing Home Assistant
    automation already owns the target entity, whether the target has been
    flip-flopped recently, and what the owner's standing intent (facts) says.
    Its question is "should this happen at all, right now?"

  * The VERIFIER (`_opus_verify`) fires AFTER all of a task's steps have
    already completed. Its question is "did the task accomplish its stated
    goal?" — a correctness check on the finished outcome, not a pre-dispatch
    safety gate on a single action.

`evaluate_action()` is the only public entry point and NEVER raises: any
exception, timeout, `BudgetExceeded`, or unparseable model response anywhere
in this module is caught and turned into a fail-safe veto result so a bug or
outage in the judge can never itself hang or crash the caller. Context is
assembled from six independently-degrading sections; a failure in any single
section degrades that section to "(none)" and never aborts the whole call.

WIRING: `evaluate_action` is called from `backend/safety/broker.py`'s
`execute_action` (the action-judge gate, after the throttle/circuit-breaker
check and before the throttle attempt is recorded). Whether a veto actually
BLOCKS anything is `settings.action_judge_mode`: "off" skips the judge
entirely, "shadow" records the verdict on the ActionLog row and dispatches
anyway, "enforce" flips the row to NEEDS_CONFIRM and pages for approval. The
mode has been "shadow" since this shipped, so every verdict here is currently
advisory — `verdict_summary()` below is what makes those advisory verdicts
visible (it feeds the daily homelab digest's Action judge section).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync DB helpers — invoked ONLY via asyncio.to_thread. Each opens/closes its
# own Session and returns plain dicts/scalars/None so no ORM object or Session
# crosses an `await` boundary. Best-effort: return None/[] on any error so the
# calling context section degrades gracefully instead of raising.
# ---------------------------------------------------------------------------

def _db_task_prompt(task_id: int) -> str | None:
    """Best-effort sync lookup of a Task's prompt text by id.

    Sync only — call via asyncio.to_thread. Returns None on any error or
    missing row.
    """
    try:
        from sqlmodel import Session

        from backend.database import Task, engine

        with Session(engine) as session:
            row = session.get(Task, task_id)
            if row is None:
                return None
            return row.prompt
    except Exception:
        return None


def _db_recent_action_log(target: str, since: datetime, limit: int = 5) -> list[dict]:
    """Last <= `limit` ActionLog rows for `target` since `since` (thrash/flip-flop
    detection). Sync only — call via asyncio.to_thread. Returns [] on any error.
    """
    try:
        from sqlmodel import Session, select

        from backend.database import ActionLog, engine

        with Session(engine) as session:
            stmt = (
                select(ActionLog)
                .where(ActionLog.target == target)
                .where(ActionLog.created_at >= since)
                .order_by(ActionLog.created_at.desc())
                .limit(limit)
            )
            rows = session.exec(stmt).all()
        return [
            {
                "kind": r.kind,
                "decision": r.decision,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    except Exception:
        return []


def _db_verdict_summary(since: datetime) -> dict:
    """Aggregate judged ActionLog rows since `since`. Sync only — call via
    asyncio.to_thread. Returns a zeroed summary on any error.

    "veto" and "error" are counted separately and deliberately never summed
    into one number: a veto is the judge's actual opinion, while "error" is
    `evaluate_action`'s fail-safe for a timeout/BudgetExceeded/unparseable
    response. In enforce mode both would block, which is exactly why they have
    to be distinguishable here — a digest reporting "12 would have been
    blocked" that turns out to be 12 judge timeouts is telling Brian the
    opposite of the truth about his automation.
    """
    zero = {"total": 0, "approve": 0, "veto": 0, "error": 0, "by_kind": {}}
    try:
        from sqlmodel import Session, select

        from backend.database import ActionLog, engine

        with Session(engine) as session:
            stmt = (
                select(ActionLog)
                .where(ActionLog.created_at >= since)
                .where(ActionLog.judge_verdict != None)  # noqa: E711
            )
            rows = session.exec(stmt).all()

        out = dict(zero, by_kind={})
        for r in rows:
            verdict = r.judge_verdict or "error"
            out["total"] += 1
            if verdict in out:
                out[verdict] += 1
            if verdict in ("veto", "error"):
                bucket = out["by_kind"].setdefault(r.kind, {"veto": 0, "error": 0})
                bucket[verdict] += 1
        return out
    except Exception as e:
        logger.warning(f"judge verdict summary failed: {e}")
        return zero


async def verdict_summary(hours: int = 24) -> dict:
    """What the action judge has been saying, over the last `hours`.

    The judge has run in shadow mode since it shipped and nothing anywhere
    aggregated its verdicts, so "what would enforce mode have blocked?" — the
    only question that can justify ever flipping the mode — had no answer short
    of querying the DB by hand. NEVER raises; degrades to a zeroed summary,
    same contract as every other read in this module.
    """
    try:
        return await asyncio.to_thread(
            _db_verdict_summary, datetime.utcnow() - timedelta(hours=hours)
        )
    except Exception as e:
        logger.warning(f"verdict_summary failed: {e}")
        return {"total": 0, "approve": 0, "veto": 0, "error": 0, "by_kind": {}}


# ---------------------------------------------------------------------------
# Context sections — each independently best-effort. A failure in one degrades
# that section's text to "(none)"; it never propagates and never aborts the
# overall evaluate_action call.
# ---------------------------------------------------------------------------

def _format_action_section(actor, kind, target, payload, risk, reversibility) -> str:
    """Section 1: the action itself (kind/target/payload/risk/reversibility/actor)."""
    try:
        actor_s = getattr(actor, "value", str(actor))
        risk_s = getattr(risk, "value", str(risk))
        rev_s = getattr(reversibility, "value", str(reversibility))
        try:
            payload_s = json.dumps(payload or {}, default=str)
        except (TypeError, ValueError):
            payload_s = str(payload)
        return (
            f"actor: {actor_s}\n"
            f"kind: {kind}\n"
            f"target: {target}\n"
            f"payload: {payload_s}\n"
            f"risk: {risk_s}\n"
            f"reversibility: {rev_s}"
        )
    except Exception:
        return "(none)"


async def _origin_context() -> str:
    """Section 2: the current task's prompt, via router._current_task_id -> Task lookup."""
    try:
        from backend.agents import router

        task_id = router._current_task_id.get()
        if task_id is None:
            return "(none)"
        prompt = await asyncio.to_thread(_db_task_prompt, task_id)
        return prompt[:300] if prompt else "(none)"
    except Exception:
        return "(none)"


async def _time_context() -> str:
    """Section 3: local time (briefing_timezone) + HA sun.sun day/night state.

    Reuses proposer._is_night(ha) rather than duplicating the sun-state logic.
    """
    try:
        from backend.agents import proposer
        from backend.config import get_settings
        from backend.integrations import homeassistant

        settings = get_settings()
        try:
            from zoneinfo import ZoneInfo

            local_now = datetime.now(ZoneInfo(settings.briefing_timezone))
            local_str = local_now.strftime("%Y-%m-%d %H:%M %Z")
        except Exception:
            local_str = "(unknown local time)"

        try:
            ha = await homeassistant.fetch()
        except Exception:
            ha = None

        is_night = proposer._is_night(ha)
        if is_night is None:
            night_str = "unknown (sun.sun unavailable)"
        else:
            night_str = "night" if is_night else "day"

        return f"local_time: {local_str}\nday_or_night: {night_str}"
    except Exception:
        return "(none)"


async def _automation_context(target: str) -> str:
    """Section 4: existing HA automations that already reference the target entity_id.

    This is the section that would have caught NEXUS turning off a Christmas
    tree plug an existing HA automation already managed — must actually
    surface in the prompt when present.
    """
    try:
        from backend.integrations import homeassistant

        index = await homeassistant.fetch_automation_index()
        names = index.get(target) or []
        if not names:
            return "(none)"
        return "\n".join(f"- {n}" for n in names)
    except Exception:
        return "(none)"


async def _history_context(target: str) -> str:
    """Section 5: last <=5 ActionLog rows for the same target in the past 24h."""
    try:
        since = datetime.utcnow() - timedelta(hours=24)
        rows = await asyncio.to_thread(_db_recent_action_log, target, since, 5)
        if not rows:
            return "(none)"
        return "\n".join(f"- {r['created_at']} {r['kind']} -> {r['decision']}" for r in rows)
    except Exception:
        return "(none)"


async def _owner_intent_context() -> str:
    """Section 6: top <=10 active Facts, reusing proposer._db_actionable_facts."""
    try:
        from backend.agents import proposer

        facts = await asyncio.to_thread(proposer._db_actionable_facts, 10)
        if not facts:
            return "(none)"
        return "\n".join(f"- {f['subject']} {f['predicate']}: {f['value']}" for f in facts)
    except Exception:
        return "(none)"


# ---------------------------------------------------------------------------
# The judge
# ---------------------------------------------------------------------------

async def evaluate_action(actor, kind, target, payload, risk, reversibility) -> dict:
    """Ask the action-judge model whether this action should be allowed to dispatch.

    Returns {"allow": bool, "confidence": float, "reason": str, "verdict": str}
    where verdict is one of "approve" | "veto" | "error" (matching
    ActionLog.judge_verdict's contract). NEVER raises: any exception, timeout,
    BudgetExceeded, or unparseable model response results in a fail-safe
    {"allow": False, "confidence": 0.0, "reason": "...", "verdict": "error"}.

    Single-shot model call only — no tool use, no agentic loop.
    """
    try:
        from backend.agents import router
        from backend.config import get_settings
        from backend.safety.governor import BudgetExceeded

        settings = get_settings()

        action_section = _format_action_section(actor, kind, target, payload, risk, reversibility)
        origin_section = await _origin_context()
        time_section = await _time_context()
        automation_section = await _automation_context(target)
        history_section = await _history_context(target)
        intent_section = await _owner_intent_context()

        prompt = (
            "You are NEXUS's action judge — a safety/context check that runs BEFORE a "
            "side-effecting action dispatches (turning something on/off, sending a message, "
            "restarting a service, etc). Decide whether this specific action should be "
            "allowed to happen right now, given everything below. Be conservative: if an "
            "existing automation already manages this target, or recent history shows this "
            "target being flip-flopped repeatedly, or the action conflicts with the owner's "
            "standing intent, lean toward NOT allowing it.\n\n"
            f"ACTION:\n{action_section}\n\n"
            f"ORIGIN (task that proposed this action):\n{origin_section}\n\n"
            f"TIME CONTEXT:\n{time_section}\n\n"
            f"EXISTING AUTOMATIONS REFERENCING THIS TARGET:\n{automation_section}\n\n"
            f"RECENT HISTORY FOR THIS TARGET (last 24h):\n{history_section}\n\n"
            f"OWNER'S STANDING INTENT (known facts):\n{intent_section}\n\n"
            "Return JSON only, no prose:\n"
            '{"allow": true|false, "confidence": 0.0-1.0, "reason": "one sentence"}'
        )

        try:
            raw = await asyncio.wait_for(
                router.run_model(settings.action_judge_model, prompt, label="action_judge"),
                timeout=settings.action_judge_timeout_s,
            )
        except asyncio.TimeoutError:
            return {
                "allow": False,
                "confidence": 0.0,
                "reason": "judge timed out",
                "verdict": "error",
            }
        except BudgetExceeded as e:
            return {
                "allow": False,
                "confidence": 0.0,
                "reason": f"judge skipped: budget exceeded ({e})",
                "verdict": "error",
            }

        # Defensive JSON extraction — same find("{")/rfind("}") pattern as
        # backend/agents/voice.py::route_intent.
        start = raw.find("{")
        end = raw.rfind("}") + 1
        parsed = json.loads(raw[start:end])

        allow = bool(parsed.get("allow"))
        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))
        reason = str(parsed.get("reason") or "").strip() or "(no reason given)"

        return {
            "allow": allow,
            "confidence": confidence,
            "reason": reason,
            "verdict": "approve" if allow else "veto",
        }

    except Exception as e:  # never raise out to the caller — fail-safe veto
        logger.warning(f"action judge evaluate_action failed (fail-safe veto): {e}")
        return {
            "allow": False,
            "confidence": 0.0,
            "reason": f"judge error: {e}",
            "verdict": "error",
        }
