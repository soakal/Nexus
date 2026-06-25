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
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# A source is anomalous only when it has been continuously down for this many
# consecutive 2-min uptime samples (~10 min). Single-sample blips are ignored.
# ponytail: module constant; make PROPOSER_MIN_OUTAGE_SAMPLES a config setting if Brian wants runtime tuning.
_MIN_OUTAGE_SAMPLES = 5


# ---------------------------------------------------------------------------
# Sync DB helpers for proposer enrichment context (own Session, best-effort).
# Called exclusively via asyncio.to_thread; return [] on any error so one
# failing sub-query degrades that prompt section to "(none)", never aborts tick.
# ---------------------------------------------------------------------------

def _db_trend_summary(since: datetime) -> list[dict]:
    """Group TrendSnapshots since `since` by (source, metric); return first/last/n per group."""
    try:
        import backend.database as _db_mod
        from sqlmodel import Session, select
        from backend.database import TrendSnapshot

        with Session(_db_mod.engine) as session:
            stmt = (
                select(TrendSnapshot)
                .where(TrendSnapshot.captured_at >= since)
                .order_by(TrendSnapshot.captured_at.asc())  # type: ignore[attr-defined]
            )
            rows = session.exec(stmt).all()

        # Group by (source, metric)
        groups: dict[tuple, list] = {}
        for r in rows:
            key = (r.source, r.metric)
            groups.setdefault(key, []).append(r.value)

        result = []
        for (source, metric), values in groups.items():
            result.append({
                "source": source,
                "metric": metric,
                "first": values[0],
                "last": values[-1],
                "n": len(values),
            })
        return result
    except Exception:
        return []


def _db_uptime_anomalies(since: datetime) -> list[dict]:
    """Return sources that are CURRENTLY DOWN and have been for >= _MIN_OUTAGE_SAMPLES.

    Only reports a source when both conditions hold:
      (a) its most recent sample is ok=False  (still down NOW)
      (b) the trailing consecutive-failure run is >= _MIN_OUTAGE_SAMPLES (~10 min)
    A source that recovered (last sample ok=True) is not reported — it self-healed,
    no investigation needed. Single transient blips are ignored.
    """
    try:
        import backend.database as _db_mod
        from sqlmodel import Session, select, asc
        from backend.database import UptimeSample

        with Session(_db_mod.engine) as session:
            stmt = (
                select(UptimeSample)
                .where(UptimeSample.checked_at >= since)
                .order_by(asc(UptimeSample.checked_at))
            )
            rows = session.exec(stmt).all()

        # Group samples by source (already ordered ascending)
        by_source: dict[str, list] = {}
        for r in rows:
            by_source.setdefault(r.source, []).append(r)

        result = []
        for src, samples in by_source.items():
            if not samples:
                continue
            # (a) still down NOW?
            if samples[-1].ok:
                continue
            # (b) count trailing consecutive failures
            trailing = 0
            for s in reversed(samples):
                if s.ok:
                    break
                trailing += 1
            if trailing >= _MIN_OUTAGE_SAMPLES:
                result.append({"source": src, "incidents": 1, "down_samples": trailing})

        return result
    except Exception:
        return []


def _ha_entity_summary(ha) -> str:
    """Compact state summary for lights, security devices, and room temps.

    Gives Opus enough context to propose 'turn off the Christmas tree plug'
    or 'garage door has been open for a while' without seeing all N entities.
    """
    if not ha or isinstance(ha, Exception):
        return "(unavailable)"
    entities = getattr(ha, "entities", []) or []
    by_id = {e.get("entity_id", ""): e for e in entities}

    WATCH = {
        "switch.tall_light_lr_christmas_tree_plug": "xmas_tree_plug",
        "light.left_porch_light": "porch_light_left",
        "light.right_porch_light": "porch_light_right",
        "light.left_garage_light": "garage_light_left",
        "light.right_garage_light": "garage_light_right",
        "cover.garage_door_garage_door": "garage_door",
        "lock.dining_room": "back_door_lock",
    }
    lines = []
    for eid, label in WATCH.items():
        ent = by_id.get(eid)
        if not ent:
            continue
        state = ent.get("state")
        if state in ("unavailable", "unknown", None):
            continue  # stale/offline entity — don't surface, don't trigger investigation
        lines.append(f"- {label}: {state}")

    # Discover room temperature sensors dynamically
    for e in entities:
        eid = e.get("entity_id", "")
        if eid.startswith("sensor.") and "temperature" in eid and e.get("state") not in ("unavailable", "unknown", None):
            try:
                temp = float(e["state"])
                label = eid.replace("sensor.", "").replace("_current_temperature", "").replace("_temperature", "").replace("_", " ")
                lines.append(f"- temp/{label}: {temp:.0f}°F")
            except (ValueError, TypeError):
                pass

    return "\n".join(lines) if lines else "(no watched entities found)"


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
        ha_entity_text = _ha_entity_summary(ha)

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
        # Gather enrichment context: trends, uptime anomalies, rejection memory.
        # Each gather is best-effort: a failure degrades that section to "(none)"
        # and NEVER aborts the tick.
        # ------------------------------------------------------------------
        now = datetime.utcnow()
        since = now - timedelta(days=7)
        # Anomalies use a 24h window so stale samples (e.g. from a since-fixed
        # infrastructure bug) don't keep triggering the same investigation goal.
        since_anomalies = now - timedelta(hours=24)

        try:
            trend_rows = await asyncio.to_thread(_db_trend_summary, since)
        except Exception:
            trend_rows = []

        try:
            anom_rows = await asyncio.to_thread(_db_uptime_anomalies, since_anomalies)
        except Exception:
            anom_rows = []

        try:
            abandoned_rows = await asyncio.to_thread(goals._db_recent_abandoned, 8)
        except Exception:
            abandoned_rows = []

        # Format trend lines: "- {source} {metric}: {first:.0f} -> {last:.0f} (rising|falling|flat)"
        if trend_rows:
            trend_lines = []
            for t in trend_rows:
                if t["last"] > t["first"]:
                    direction = "rising"
                elif t["last"] < t["first"]:
                    direction = "falling"
                else:
                    direction = "flat"
                trend_lines.append(
                    f"- {t['source']} {t['metric']}: {t['first']:.0f} -> {t['last']:.0f} ({direction})"
                )
            trends_text = "\n".join(trend_lines)
        else:
            trends_text = "(none)"

        # Format anomaly lines: "- {source}: N outage incident(s)"
        if anom_rows:
            anoms_text = "\n".join(
                f"- {a['source']}: {a['incidents']} outage incident(s)" for a in anom_rows
            )
        else:
            anoms_text = "(none)"

        # Format abandoned lines: "- {title} — {reason}" (reason omitted if None)
        if abandoned_rows:
            abandoned_lines = []
            for ab in abandoned_rows:
                line = f"- {ab['title']}"
                if ab.get("rejection_reason"):
                    line += f" — {ab['rejection_reason']}"
                abandoned_lines.append(line)
            abandoned_text = "\n".join(abandoned_lines)
        else:
            abandoned_text = "(none)"

        # ------------------------------------------------------------------
        # Ask Opus to propose new goals.
        # ------------------------------------------------------------------
        prompt = (
            "You are NEXUS's planning daemon. Review the live homelab state and the goals already open.\n"
            "Propose any NEW objectives genuinely worth doing — concrete, actionable, NOT already open,\n"
            "NOT destructive. Be conservative: return an EMPTY array unless something clearly warrants\n"
            "attention (e.g. storage near full, a device alert, many stale PRs, a light left on,\n"
            "the garage door open, or the back door unlocked). Never propose anything already open.\n"
            "Use the RECENT TRENDS to anticipate upcoming issues (e.g. storage rising toward full,\n"
            "blocked percentage climbing). Check HA ENTITY STATES for devices that may have been\n"
            "left on/open/unlocked — the home_control write tool can turn them off (low-risk, reversible).\n"
            "Do NOT propose anything on the DO NOT RE-PROPOSE list — Brian explicitly rejected those.\n\n"
            f"Return JSON only — an array (max {max_per_tick}) of:\n"
            '[{"title": "...", "description": "concrete goal the executor can pursue", '
            '"risk": "low|medium|high", '
            '"reversibility": "reversible|reversible_by_inverse|irreversible|unknown", '
            '"confidence": 0.0-1.0, '
            '"category": "one of: maintenance|storage|network|media|monitoring|knowledge|other"}]\n'
            "Empty array [] if nothing warrants action.\n\n"
            f"LIVE STATE:\n{snapshot}\n\n"
            f"HA ENTITY STATES (lights, security — check for left-on/open/unlocked):\n{ha_entity_text}\n\n"
            f"ALREADY-OPEN GOALS (do NOT duplicate):\n{open_goals_text}\n\n"
            f"RECENT TRENDS (7d):\n{trends_text}\n\n"
            f"UPTIME ANOMALIES (24h, outage incidents):\n{anoms_text}\n\n"
            f"DO NOT RE-PROPOSE (recently rejected/abandoned by Brian — respect his judgment):\n{abandoned_text}"
        )

        try:
            raw = await router.sonnet(prompt, label="goal_proposer")
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
                category=goals.normalize_category(item.get("category")),
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
                    if entry["auto_approved"]:
                        from backend import events
                        await events.notify_phone(
                            f"NEXUS auto-started a low-risk goal: {title}",
                            kind="auto_approved",
                        )
                except Exception as _ae:
                    logger.warning(
                        "goal_proposer: auto-approve failed for goal %s: %s",
                        res["goal"].get("id"), _ae,
                    )
                    entry["auto_approved"] = False

            # Notify for goals that need human approval (not auto-approved, not duplicates).
            elif res["status"] == "proposed":
                from backend import events
                await events.notify_phone(
                    f"New goal needs your approval: {title}\nRisk: {risk} — open Goals tab to review.",
                    kind="goal_proposed",
                )

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
