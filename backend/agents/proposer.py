"""Autonomous goal proposer with narrow auto-approve (Tier 3).

A scheduled tick reviews live homelab state + already-open goals and asks Opus to
PROPOSE new objectives. Goals are created in status 'proposed' (actor='autonomous').
When auto_approve_low_risk is True, goals that pass goals.is_auto_approvable() —
i.e. low risk + reversible + autonomous actor — are immediately approved via
goals.approve(); all other goals (medium/high risk, irreversible, human-proposed,
or flag-off) stay 'proposed' and await human approval in the Safety UI.
Gated by the kill switch: when autonomy_enabled is False the tick is a no-op.
Best-effort: never raises (it is a scheduler job).

SAFETY CONTRACT (hard, enforced at code level):
  - Calls goals.propose() for every candidate.
  - MAY call goals.approve(), but ONLY for goals that pass goals.is_auto_approvable()
    (low risk + reversible + autonomous actor + flag on). All other goals stay
    'proposed'. NEVER approves MEDIUM/HIGH/irreversible/human goals.
  - NEVER calls execute_action, run_task, or get_pool directly.
  - Gated by governor.get_system_state()["autonomy_enabled"] before any Opus call.
  - Even auto-approved goals' tasks are still broker-gated per-action (actor="agent"),
    so a HIGH/irreversible action mid-task is still blocked/needs-confirm.
"""
import asyncio
import json
import logging

logger = logging.getLogger(__name__)


async def propose_goals_tick() -> dict:
    """Review live homelab state and propose new goals via Opus (suggest-only).

    Returns a result dict with 'status': 'ok' | 'skipped' | 'error'.
    Never raises — all exceptions are caught and returned as status='error'.
    """
    try:
        from backend.safety import governor
        from backend.safety.governor import BudgetExceeded
        from backend.agents import goals, router
        from backend.agents.chat import _build_snapshot
        from backend.config import get_settings
        from backend.integrations import (
            homeassistant, unraid, channels_dvr, adguard, weather,
        )

        # ------------------------------------------------------------------
        # Kill switch — first guard. Check BEFORE any Opus call or DB write.
        # ------------------------------------------------------------------
        state = await asyncio.to_thread(governor.get_system_state)
        if not state["autonomy_enabled"]:
            return {"status": "skipped", "reason": "autonomy_disabled"}

        # ------------------------------------------------------------------
        # Gather live homelab state.
        # ------------------------------------------------------------------
        results = await asyncio.gather(
            homeassistant.fetch(),
            unraid.fetch(),
            channels_dvr.fetch(),
            adguard.fetch(),
            weather.fetch(),
            return_exceptions=True,
        )
        ha, unraid_d, channels, ag, wx = results
        snapshot = _build_snapshot(ha, unraid_d, channels, ag, wx)

        # ------------------------------------------------------------------
        # Load already-open goals so Opus avoids duplicates.
        # ------------------------------------------------------------------
        goals_list = await asyncio.to_thread(goals._db_list_goals, None, 25)
        open_goals_lines = "\n".join(
            f"[{g['status']}] {g['title']}"
            for g in goals_list
            if g["status"] in ("proposed", "approved", "running")
        )
        open_goals_text = open_goals_lines or "(none)"

        s = get_settings()
        max_per_tick = s.proposer_max_per_tick
        auto_approve_enabled = s.auto_approve_low_risk

        # ------------------------------------------------------------------
        # Ask Opus to propose new goals.
        # ------------------------------------------------------------------
        prompt = (
            "You are NEXUS's planning daemon. Review the live homelab state and the goals already open.\n"
            "Propose any NEW objectives genuinely worth doing — concrete, actionable, NOT already open,\n"
            "NOT destructive. Be conservative: return an EMPTY array unless something clearly warrants\n"
            "attention (e.g. storage near full, a device alert, many stale PRs). Never propose anything\n"
            "that is already listed as open.\n\n"
            f"Return JSON only — an array (max {max_per_tick}) of:\n"
            '[{"title": "...", "description": "concrete goal the executor can pursue", '
            '"risk": "low|medium|high", '
            '"reversibility": "reversible|reversible_by_inverse|irreversible|unknown", '
            '"confidence": 0.0-1.0}]\n'
            "Empty array [] if nothing warrants action.\n\n"
            f"LIVE STATE:\n{snapshot}\n\n"
            f"ALREADY-OPEN GOALS (do NOT duplicate):\n{open_goals_text}"
        )

        try:
            raw = await router.opus(prompt, label="goal_proposer")
        except BudgetExceeded:
            return {"status": "skipped", "reason": "budget"}

        # ------------------------------------------------------------------
        # Parse the JSON array defensively.
        # ------------------------------------------------------------------
        proposals: list = []
        try:
            start_idx = raw.find("[")
            end_idx = raw.rfind("]")
            if start_idx >= 0 and end_idx > start_idx:
                parsed = json.loads(raw[start_idx : end_idx + 1])
                if isinstance(parsed, list):
                    proposals = parsed
        except Exception:
            proposals = []

        # Cap to max_per_tick.
        proposals = proposals[:max_per_tick]

        # ------------------------------------------------------------------
        # Propose each valid item; auto-approve if policy permits.
        # ------------------------------------------------------------------
        results_list = []
        for item in proposals:
            title = str(item.get("title") or "").strip()
            description = str(item.get("description") or "").strip()
            if not title or not description:
                continue

            risk = str(item.get("risk") or "medium")
            reversibility = str(item.get("reversibility") or "unknown")
            try:
                confidence = float(item.get("confidence") or 0.6)
            except (TypeError, ValueError):
                confidence = 0.6

            res = await goals.propose(
                title,
                description,
                actor="autonomous",
                confidence=confidence,
                risk=risk,
                reversibility=reversibility,
                ttl_seconds=s.goal_ttl_seconds,
                debounce_seconds=s.goal_debounce_seconds,
            )
            entry = {
                "title": title,
                "status": res["status"],
                "reason": res.get("reason"),
            }

            # Narrow auto-approve: only when the propose actually created a new
            # goal AND all four policy conditions hold (autonomous + low + reversible
            # + flag on). Human-proposed, medium/high, irreversible, flag-off →
            # entry stays "proposed" and waits for a human in the Safety UI.
            if res["status"] == "proposed" and goals.is_auto_approvable(
                res["goal"], enabled=auto_approve_enabled
            ):
                try:
                    appr = await goals.approve(
                        res["goal"]["id"],
                        approved_by="auto:low_risk_reversible",
                    )
                    entry["auto_approved"] = (appr.get("status") == "approved")
                    entry["status"] = (
                        "auto_approved" if appr.get("status") == "approved"
                        else res["status"]
                    )
                except Exception as _ae:
                    logger.warning(
                        "goal_proposer: auto-approve failed for goal %s: %s",
                        res["goal"].get("id"), _ae,
                    )
                    entry["auto_approved"] = False

            results_list.append(entry)

        count_proposed = sum(1 for r in results_list if r["status"] == "proposed")
        count_auto_approved = sum(1 for r in results_list if r.get("auto_approved") is True)
        logger.info(
            "goal_proposer tick done: %d proposed, %d auto-approved, %d total evaluated",
            count_proposed,
            count_auto_approved,
            len(results_list),
        )
        return {
            "status": "ok",
            "results": results_list,
            "count_proposed": count_proposed,
            "count_auto_approved": count_auto_approved,
        }

    except Exception as e:
        logger.warning("propose_goals_tick failed (best-effort): %s", e)
        return {"status": "error", "error": str(e)}
