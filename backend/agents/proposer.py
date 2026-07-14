"""Autonomous goal proposer with narrow auto-approve (Tier 3).

A scheduled tick reviews live homelab state + already-open goals and asks Haiku to
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
  - Gated by governor.get_system_state()["autonomy_enabled"] before any LLM call.
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

# Structural capability gaps — things NEXUS's read-only executor cannot do no
# matter how a goal is worded. Fed into the prompt so the proposer stops
# re-learning this goal-by-goal via repeated RECENTLY FAILED entries (each
# failure still costs a proposal + a failure notification even with that fix).
# Update this list whenever a new structural gap is discovered via a failed goal.
NEXUS_CANNOT = [
    "configure Unraid storage alerts/notifications",
    "get per-device UniFi client metrics (only aggregate client count/uplink status)",
    "apply/upgrade Proxmox packages (can only refresh the apt index, not install)",
    "configure Home Assistant automations or scenes",
    "modify router/firewall rules",
]


# ---------------------------------------------------------------------------
# Sync DB helpers for proposer enrichment context (own Session, best-effort).
# Called exclusively via asyncio.to_thread; return [] on any error so one
# failing sub-query degrades that prompt section to "(none)", never aborts tick.
# ---------------------------------------------------------------------------

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


# Module-level so the night-exemption filter in propose_goals_tick can share
# the exact entity_id<->label mapping used to build the LLM's context.
WATCH = {
    # switch.tall_light_lr_christmas_tree_plug (Christmas tree plug) intentionally
    # excluded (Brian, 2026-07-14): an HA automation already turns it off nightly at
    # 11:59pm, so NEXUS must NOT propose/auto-off it during the day (it's on by
    # intent, and it's already handled). Do not re-add without that automation gone.
    "light.left_porch_light": "porch_light_left",
    "light.right_porch_light": "porch_light_right",
    "light.left_garage_light": "garage_light_left",
    "light.right_garage_light": "garage_light_right",
    "cover.garage_door_garage_door": "garage_door",
    "lock.dining_room": "back_door_lock",
}

# Brian leaves these exterior lights on overnight on purpose (security lighting) —
# never auto-off them at night, regardless of what the LLM proposes.
NIGHT_EXEMPT_LABELS = {"porch_light_left", "porch_light_right", "garage_light_left", "garage_light_right"}
NIGHT_EXEMPT_ENTITY_IDS = {eid for eid, label in WATCH.items() if label in NIGHT_EXEMPT_LABELS}

# TEMPORARY (added 2026-07-09, Brian): the porch light circuit has water damage
# and is only reliably operable via the physical wall switch right now -- HA
# service calls against it are unreliable (this is why goal #59, "turn off
# left/right porch lights", failed verification: the executor's turn_off calls
# got an ambiguous empty response and the lights were still on/unreachable
# minutes later). Both left/right are exempted since it's unclear which one
# Brian means by "front porch light" and both currently show state=unavailable
# in HA. Suppresses BOTH goal proposals and the resulting failure notifications
# until Brian says it's fixed -- REMOVE this exemption then, don't leave it.
KNOWN_HARDWARE_ISSUE_LABELS: set[str] = {"porch_light_left", "porch_light_right"}
KNOWN_HARDWARE_ISSUE_ENTITY_IDS = {eid for eid, label in WATCH.items() if label in KNOWN_HARDWARE_ISSUE_LABELS}
# Fallback only — used when the sun.sun entity is missing/unavailable. A fixed
# clock hour is a poor stand-in for dawn (Detroit sunrise ranges ~6am midsummer
# to ~8am midwinter), so the live sun.sun entity (below_horizon/above_horizon)
# is the primary source; see _is_night().
NIGHT_START_HOUR = 20  # 8pm
NIGHT_END_HOUR = 7     # 7am


def _is_night(ha) -> bool | None:
    """True/False from HA's live sun.sun entity (below_horizon = night, tracks
    actual dawn/dusk for the configured lat/long). None if unavailable —
    caller falls back to the fixed-hour heuristic."""
    if not ha or isinstance(ha, Exception):
        return None
    for e in getattr(ha, "entities", []) or []:
        if e.get("entity_id") == "sun.sun":
            state = e.get("state")
            if state in ("above_horizon", "below_horizon"):
                return state == "below_horizon"
            return None
    return None


def _ha_entity_summary(ha) -> str:
    """Compact state summary for lights, security devices, and room temps.

    Gives the proposer enough context to propose e.g. 'a garage light was left
    on' or 'garage door has been open for a while' without seeing all N entities.
    """
    if not ha or isinstance(ha, Exception):
        return "(unavailable)"
    entities = getattr(ha, "entities", []) or []
    by_id = {e.get("entity_id", ""): e for e in entities}

    lines = []
    for eid, label in WATCH.items():
        ent = by_id.get(eid)
        if not ent:
            continue
        state = ent.get("state")
        if state in ("unavailable", "unknown", None):
            continue  # stale/offline entity — don't surface, don't trigger investigation
        # Real entity_id included so the executor (which only sees this goal's
        # description text, no live HA lookup) can act on it verbatim instead
        # of guessing an entity_id back from the friendly label.
        lines.append(f"- {label} (entity_id: {eid}): {state}")

    # Discover room temperature sensors dynamically (shared with tools.py's
    # homeassistant_temperatures so a new sensor shows up in both automatically).
    from backend.agents.chat import extract_temperature_sensors
    for t in extract_temperature_sensors(ha):
        lines.append(f"- temp/{t['label']}: {t['value_f']:.0f}°F")

    return "\n".join(lines) if lines else "(no watched entities found)"


def _db_actionable_facts(limit: int = 12) -> list[dict]:
    """Active, non-dismissed facts NOT sourced from a completed goal's own outcome.

    Excluding source=="task" is the loop-break: goal_outcome_distill_llm (default
    True) writes completed-goal outcomes as source="task" facts -- surfacing
    those back to the proposer would let a completed goal spawn a near-identical
    new proposal. Called via asyncio.to_thread; best-effort, returns [] on error.
    """
    try:
        import backend.database as _db_mod
        from sqlmodel import Session, select
        from backend.database import Fact
        from backend.agents.facts import effective_confidence, EFFECTIVE_FLOOR

        now = datetime.utcnow()
        with Session(_db_mod.engine) as session:
            stmt = (
                select(Fact)
                .where(Fact.superseded_by == None)  # noqa: E711
                .where(Fact.dismissed_at == None)  # noqa: E711
                .where(Fact.source != "task")
                .order_by(Fact.updated_at.desc())  # type: ignore[attr-defined]
                .limit(limit * 2)  # over-fetch: some may be below the confidence floor
            )
            rows = session.exec(stmt).all()

        result = []
        for r in rows:
            age_days = (now - r.updated_at).total_seconds() / 86400
            if effective_confidence(r.confidence, age_days, source=r.source) < EFFECTIVE_FLOOR:
                continue
            result.append({"subject": r.subject, "predicate": r.predicate, "value": r.value})
            if len(result) >= limit:
                break
        return result
    except Exception:
        return []


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

        # Retire proposals past their TTL every tick (runs even when autonomy
        # is off, below) so stale proposals can't pile up in the Safety UI /
        # digest -- expiry used to be applied only lazily inside approve().
        try:
            await goals.expire_stale_proposals()
        except Exception as _e:
            logger.debug("proposer: expire_stale_proposals failed (best-effort): %s", _e)

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

        sun_night = _is_night(ha)
        if sun_night is not None:
            is_night = sun_night  # tracks actual dawn/dusk, not a guessed clock hour
            local_now = datetime.utcnow()
        else:
            # sun.sun entity unavailable — fall back to a fixed clock window.
            # If even that can't be computed (bad/mocked timezone setting),
            # default to daytime: a missed exemption is safer than an
            # incorrectly-forced one (worst case is a redundant notification,
            # not lights being cut on Brian while he's relying on them).
            try:
                from zoneinfo import ZoneInfo
                local_now = datetime.now(ZoneInfo(s.briefing_timezone))
                is_night = local_now.hour >= NIGHT_START_HOUR or local_now.hour < NIGHT_END_HOUR
            except Exception:
                local_now = datetime.utcnow()
                is_night = False

        # ------------------------------------------------------------------
        # Gather enrichment context: uptime anomalies, rejection memory.
        # Each gather is best-effort: a failure degrades that section to "(none)"
        # and NEVER aborts the tick.
        # ------------------------------------------------------------------
        now = datetime.utcnow()
        # Anomalies use a 24h window so stale samples (e.g. from a since-fixed
        # infrastructure bug) don't keep triggering the same investigation goal.
        since_anomalies = now - timedelta(hours=24)

        try:
            anom_rows = await asyncio.to_thread(_db_uptime_anomalies, since_anomalies)
        except Exception:
            anom_rows = []

        try:
            abandoned_rows = await asyncio.to_thread(goals._db_recent_abandoned, 8)
        except Exception:
            abandoned_rows = []

        try:
            completed_rows = await asyncio.to_thread(goals._db_recent_completed, 12)
        except Exception:
            completed_rows = []

        try:
            failed_rows = await asyncio.to_thread(goals._db_recent_failed, 8)
        except Exception:
            failed_rows = []

        try:
            fact_rows = await asyncio.to_thread(_db_actionable_facts, 12)
        except Exception:
            fact_rows = []

        # Format anomaly lines: "- {source}: N outage incident(s)"
        if anom_rows:
            anoms_text = "\n".join(
                f"- {a['source']}: {a['incidents']} outage incident(s)" for a in anom_rows
            )
        else:
            anoms_text = "(none)"

        # Format completed lines: "- {title}"
        if completed_rows:
            completed_text = "\n".join(f"- {c['title']}" for c in completed_rows)
        else:
            completed_text = "(none)"

        # Format failed lines: "- {title} — {reason}" (reason omitted if None)
        if failed_rows:
            failed_lines = []
            for fr in failed_rows:
                line = f"- {fr['title']}"
                if fr.get("rejection_reason"):
                    line += f" — {fr['rejection_reason']}"
                failed_lines.append(line)
            failed_text = "\n".join(failed_lines)
        else:
            failed_text = "(none)"

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

        # Format fact lines: "- {subject} {predicate}: {value}"
        if fact_rows:
            facts_text = "\n".join(f"- {f['subject']} {f['predicate']}: {f['value']}" for f in fact_rows)
        else:
            facts_text = "(none)"

        # ------------------------------------------------------------------
        # Ask Haiku to propose new goals.
        # ------------------------------------------------------------------
        prompt = (
            "You are NEXUS's planning daemon. Review the live homelab state and the goals already open.\n"
            "Propose any NEW objectives genuinely worth doing — concrete, actionable, NOT already open,\n"
            "NOT destructive. Be conservative: return an EMPTY array unless something clearly warrants\n"
            "attention (e.g. storage near full, a device alert, many stale PRs, a light left on,\n"
            "the garage door open, or the back door unlocked). Never propose anything already open.\n"
            "Check HA ENTITY STATES for devices that may have been\n"
            "left on/open/unlocked. Lights and plugs can be turned off autonomously (low-risk, reversible).\n"
            "IMPORTANT: lock, alarm, and cover (garage door) are physical-security domains — classify\n"
            "these as risk='high', reversibility='irreversible' so a human must approve them.\n"
            "Temperature sensors on network/PoE gear (switches, APs, routers) normally run 40-60°C\n"
            "(104-140°F) under load — that is expected, not an anomaly. Only flag a device temperature\n"
            "if it is clearly excessive for that kind of hardware (60-70°C+/150°F+), not just because\n"
            "the raw number sounds high for a room.\n"
            "Do NOT propose anything on the DO NOT RE-PROPOSE list — Brian explicitly rejected those.\n"
            "Do NOT re-propose anything on the RECENTLY COMPLETED list unless live state shows\n"
            "the condition has clearly recurred.\n"
            "Do NOT re-propose anything on the RECENTLY FAILED list under a different wording —\n"
            "if the reason shows the executor lacks a capability (e.g. no per-device metrics, no\n"
            "way to configure an alert/policy), rewording the same idea will fail again identically.\n"
            "Do NOT propose a goal that requires anything on the NEXUS CURRENTLY CANNOT list below —\n"
            "it will fail identically to past attempts, regardless of wording.\n"
            "The executor only ever sees this goal's title+description text, not live HA state —\n"
            "so for any goal that turns a device on/off, the description MUST include the exact\n"
            "entity_id(s) from HA ENTITY STATES verbatim (e.g. 'light.left_garage_light'), not just\n"
            "the friendly label.\n"
            "KNOWN FACTS may imply a low-risk maintenance goal (e.g. 'back door lock needs battery'\n"
            "-> propose replacing it) — only propose from a fact when it clearly needs action; most\n"
            "facts are just background context, not a todo.\n"
            "The porch lights (light.left_porch_light, light.right_porch_light) have a KNOWN HARDWARE\n"
            "ISSUE (water damage, operable only via the physical wall switch right now) — do NOT\n"
            "propose ANYTHING about these two lights (turning off, investigating, or otherwise),\n"
            "even if HA ENTITY STATES shows them on/unavailable. This is temporary until Brian\n"
            "confirms the repair.\n"
            + (
                "It is currently NIGHTTIME (local time {:%H:%M}). Brian leaves the porch and garage\n"
                "lights on overnight ON PURPOSE as security lighting — do NOT propose turning off\n"
                "porch_light_left, porch_light_right, garage_light_left, or garage_light_right while\n"
                "nighttime holds, even though HA ENTITY STATES shows them on. Only propose turning them\n"
                "off if they are still on well after sunrise (daytime).\n\n".format(local_now)
                if is_night else
                "(daytime — normal left-on-light rules apply to porch/garage lights)\n\n"
            )
            + f"Return JSON only, no prose — an array (max {max_per_tick}) of:\n"
            '[{"title": "...", "description": "concrete goal the executor can pursue", '
            '"success_criteria": "a concrete, checkable statement of what DONE looks like", '
            '"risk": "low|medium|high", '
            '"reversibility": "reversible|reversible_by_inverse|irreversible|unknown", '
            '"confidence": 0.0-1.0, '
            '"category": "one of: maintenance|storage|network|media|monitoring|knowledge|other"}]\n'
            "Every goal MUST include a non-empty success_criteria.\n"
            "success_criteria MUST be checkable by the executor's own remote read-only\n"
            "tools (HA/UniFi/Unraid/etc. reads) — never require physical/on-site\n"
            "inspection (checking a fan is spinning, feeling for heat, being physically\n"
            "present). A goal whose criteria can only be confirmed in person will fail\n"
            "verification every time regardless of how well the investigation itself goes.\n"
            "Empty array [] if nothing warrants action.\n\n"
            f"LIVE STATE:\n{snapshot}\n\n"
            f"HA ENTITY STATES (lights, security — check for left-on/open/unlocked):\n{ha_entity_text}\n\n"
            f"ALREADY-OPEN GOALS (do NOT duplicate):\n{open_goals_text}\n\n"
            f"RECENTLY COMPLETED (already ran successfully — do NOT re-propose unless recurred):\n{completed_text}\n\n"
            f"RECENTLY FAILED (do NOT reword and re-propose — see reason):\n{failed_text}\n\n"
            "NEXUS CURRENTLY CANNOT (do not propose goals that require these):\n"
            + "\n".join(f"- {c}" for c in NEXUS_CANNOT) + "\n\n"
            f"UPTIME ANOMALIES (24h, outage incidents):\n{anoms_text}\n\n"
            f"KNOWN FACTS (may imply a low-risk maintenance goal):\n{facts_text}\n\n"
            f"DO NOT RE-PROPOSE (recently rejected/abandoned by Brian — respect his judgment):\n{abandoned_text}"
        )

        try:
            raw = await router.haiku(prompt, label="goal_proposer")
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
            success_criteria = str(item.get("success_criteria") or "").strip()
            if not title or not description or not success_criteria:
                # A goal without a checkable done-condition can never be
                # honestly completed — drop it (conservative).
                logger.debug(
                    "proposer: dropped goal without success_criteria: %r", title
                )
                continue

            # Deterministic backstop — never rely on the LLM alone to honor the
            # nighttime-lighting instruction above. If it's night and the goal
            # text references an exempt porch/garage light, drop it outright.
            if is_night:
                haystack = f"{title} {description}".lower()
                if any(tok in haystack for tok in NIGHT_EXEMPT_ENTITY_IDS | NIGHT_EXEMPT_LABELS):
                    logger.info(
                        "proposer: dropped night-exempt light goal: %r", title
                    )
                    continue

            # Deterministic backstop for the known-hardware-issue porch lights
            # (see KNOWN_HARDWARE_ISSUE_LABELS above) -- unconditional, not
            # gated by time of day. Never rely on the LLM alone to honor this.
            haystack = f"{title} {description}".lower()
            if any(tok in haystack for tok in KNOWN_HARDWARE_ISSUE_ENTITY_IDS | KNOWN_HARDWARE_ISSUE_LABELS):
                logger.info(
                    "proposer: dropped known-hardware-issue light goal: %r", title
                )
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
                success_criteria=success_criteria,
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
                goal_id = res["goal"]["id"]
                await events.notify_phone(
                    f"New goal needs your approval: {title}\nRisk: {risk}",
                    kind="goal_proposed",
                    buttons=[
                        {"text": "✓ Approve", "callback_data": f"goal:approve:{goal_id}"},
                        {"text": "✗ Reject", "callback_data": f"goal:reject:{goal_id}"},
                    ],
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
