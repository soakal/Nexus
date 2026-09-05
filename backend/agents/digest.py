"""Daily autonomy digest (Tier 3 phone notifications).

Builds and delivers a concise summary of what NEXUS ran autonomously, what is
awaiting human approval, today's spend, and the current autonomy state.

Sent once daily via events.notify_phone (-> Telegram).

Best-effort throughout: any sub-fetch failure degrades that line to a safe
default; the function NEVER raises.  All sync DB helpers are invoked via
asyncio.to_thread — no Session/ORM crosses an await boundary.
"""
import asyncio
import html
import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync DB helpers — call ONLY via asyncio.to_thread
# ---------------------------------------------------------------------------

def _db_recent_autonomous_goals(since: datetime) -> list[dict]:
    """Goals auto-approved in the last 24 h (approved_by=="auto:low_risk_reversible")."""
    from sqlmodel import Session, select
    from backend.database import Goal, engine

    try:
        with Session(engine) as session:
            rows = session.exec(
                select(Goal)
                .where(Goal.approved_by == "auto:low_risk_reversible")
                .where(Goal.updated_at >= since)
                .order_by(Goal.updated_at.desc())
                .limit(20)
            ).all()
            return [{"title": r.title, "status": r.status} for r in rows]
    except Exception as e:
        logger.debug(f"digest._db_recent_autonomous_goals failed: {e}")
        return []


def _db_recent_completed_goals(since: datetime) -> list[dict]:
    """Goals completed in the last 24 h, with their outcome summaries."""
    from sqlmodel import Session, select
    from backend.database import Goal, engine

    try:
        with Session(engine) as session:
            rows = session.exec(
                select(Goal)
                .where(Goal.status == "completed")
                .where(Goal.updated_at >= since)
                .order_by(Goal.updated_at.desc())
                .limit(10)
            ).all()
            return [
                {"title": r.title, "outcome_summary": r.outcome_summary}
                for r in rows
            ]
    except Exception as e:
        logger.debug(f"digest._db_recent_completed_goals failed: {e}")
        return []


def _db_recent_failed_goals(since: datetime) -> list[dict]:
    """Goals that failed in the last 24 h, with their rejection reason."""
    from sqlmodel import Session, select
    from backend.database import Goal, engine

    try:
        with Session(engine) as session:
            rows = session.exec(
                select(Goal)
                .where(Goal.status == "failed")
                .where(Goal.updated_at >= since)
                .order_by(Goal.updated_at.desc())
                .limit(10)
            ).all()
            return [
                {"title": r.title, "rejection_reason": r.rejection_reason}
                for r in rows
            ]
    except Exception as e:
        logger.debug(f"digest._db_recent_failed_goals failed: {e}")
        return []


def _db_pending_confirm_count() -> int:
    """Count of ActionLog rows with decision == 'needs_confirm'."""
    from sqlmodel import Session, func, select
    from backend.database import ActionLog, engine

    try:
        with Session(engine) as session:
            count = session.exec(
                select(func.count(ActionLog.id))
                .where(ActionLog.decision == "needs_confirm")
            ).one()
            return int(count or 0)
    except Exception as e:
        logger.debug(f"digest._db_pending_confirm_count failed: {e}")
        return 0


def _db_proposed_goals() -> list[dict]:
    """Goals with status == 'proposed', newest first, up to 10."""
    from sqlmodel import Session, select
    from backend.database import Goal, engine

    try:
        with Session(engine) as session:
            rows = session.exec(
                select(Goal)
                .where(Goal.status == "proposed")
                .order_by(Goal.created_at.desc())
                .limit(10)
            ).all()
            return [{"title": r.title, "risk": r.risk, "category": r.category} for r in rows]
    except Exception as e:
        logger.debug(f"digest._db_proposed_goals failed: {e}")
        return []


_HASH_SUFFIX = re.compile(r"^[0-9a-f]{8}$")


def _humanize_vault_check(check: str) -> str:
    """`vault_signals` checks are relay_vault_signals.py's raw hyphenated
    slugs -- e.g. "homelab:open-unresolved-items-ha-has-a-large-never-triag-
    ecab492f" -- built to be a unique fingerprint, not something a human
    reads on a phone. Only called for a check that's about to get its own
    bullet in the calibration line (see build_autonomy_digest), so this
    trades the fingerprint's precision for a skimmable label: drop the
    optional "<category>:" tag, drop the trailing content-hash word, turn
    the rest into space-separated words, and cap it at 6 of them."""
    check = check.split(":", 1)[1] if ":" in check else check
    words = check.split("-")
    if words and _HASH_SUFFIX.match(words[-1]):
        words = words[:-1]
    label = " ".join(words[:6])
    return label + "…" if len(words) > 6 else label


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------

async def build_autonomy_digest() -> str:
    """Compute and format the daily autonomy digest text.

    Best-effort: any sub-fetch failure degrades that line to a safe default.
    Never raises.
    """
    try:
        since = datetime.utcnow() - timedelta(hours=24)
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

        # Retire proposals past their TTL BEFORE reading the proposed list, so
        # the digest never shows a weeks-old expired proposal (e.g. a garage-
        # door-open alert whose door was closed long ago) as if it were current.
        try:
            from backend.agents import goals
            await goals.expire_stale_proposals()
        except Exception as e:
            logger.debug(f"digest: expire_stale_proposals failed (best-effort): {e}")

        # Fan-out all DB reads + governor calls concurrently.
        from backend.safety import governor
        from backend.agents import calibration as calibration_mod
        from backend.agents import outcomes

        auto_goals_task = asyncio.to_thread(_db_recent_autonomous_goals, since)
        pending_count_task = asyncio.to_thread(_db_pending_confirm_count)
        proposed_goals_task = asyncio.to_thread(_db_proposed_goals)
        completed_goals_task = asyncio.to_thread(_db_recent_completed_goals, since)
        spend_task = asyncio.to_thread(governor.today_spend_usd)
        state_task = asyncio.to_thread(governor.get_system_state)
        calibration_task = outcomes.calibration_summary(30)
        hint_report_task = calibration_mod.hint_report(30)
        proposer_stats_task = asyncio.to_thread(governor.get_proposer_tick_stats, 24)
        failed_goals_task = asyncio.to_thread(_db_recent_failed_goals, since)

        results = await asyncio.gather(
            auto_goals_task,
            pending_count_task,
            proposed_goals_task,
            completed_goals_task,
            spend_task,
            state_task,
            calibration_task,
            hint_report_task,
            proposer_stats_task,
            failed_goals_task,
            return_exceptions=True,
        )

        _labels = (
            "auto_goals", "pending_count", "proposed_goals", "completed_goals",
            "spend", "state", "calibration", "hint_report", "proposer_stats",
            "failed_goals",
        )
        for _label, _r in zip(_labels, results):
            if isinstance(_r, Exception):
                logger.warning(f"digest gather task {_label!r} failed (degrading): {_r}")

        auto_goals = results[0] if not isinstance(results[0], Exception) else []
        pending_count = results[1] if not isinstance(results[1], Exception) else 0
        proposed_goals = results[2] if not isinstance(results[2], Exception) else []
        completed_goals = results[3] if not isinstance(results[3], Exception) else []
        spend = results[4] if not isinstance(results[4], Exception) else 0.0
        state = results[5] if not isinstance(results[5], Exception) else {}
        calibration = results[6] if not isinstance(results[6], Exception) else {}
        hint_report = results[7] if not isinstance(results[7], Exception) else None
        proposer_stats = results[8] if not isinstance(results[8], Exception) else None
        failed_goals = results[9] if not isinstance(results[9], Exception) else []

        autonomy_label = "ENABLED" if state.get("autonomy_enabled", True) else "PAUSED"
        daily_cap = state.get("daily_budget_usd", 25.0)

        # Format auto-ran goals line.
        if auto_goals:
            goal_parts = ", ".join(
                f"{g['title']} ({g['status']})" for g in auto_goals[:5]
            )
            auto_ran_line = f"{len(auto_goals)} — {goal_parts}"
        else:
            auto_ran_line = "none"

        # Format proposed goals block.
        if proposed_goals:
            proposed_titles = "\n    - " + "\n    - ".join(
                f"{g['title']} [{html.escape(g['category'] or 'other')}]" for g in proposed_goals[:5]
            )
        else:
            proposed_titles = "\n    (none)"

        # Completed goals with their distilled outcomes — the "what actually
        # got done" line that used to vanish into result_json.
        if completed_goals:
            completed_parts = "\n    - " + "\n    - ".join(
                f"{g['title']}: {g['outcome_summary'] or 'done'}"
                for g in completed_goals[:5]
            )
            completed_line = f"Completed (24h): {len(completed_goals)}{completed_parts}"
        else:
            completed_line = "Completed (24h): none"

        # B6: failed goals with their rejection reason -- the same "what
        # actually happened" visibility completed_line gives successes.
        # rejection_reason is written on every failed goal (goals.py); only
        # auto-approved-failure notifications page, so this is often the
        # first place a quiet failure becomes visible at all. Escaped:
        # rejection_reason can carry verifier free text, sent parse_mode="HTML".
        if failed_goals:
            failed_parts = "\n    - " + "\n    - ".join(
                f"{html.escape(g['title'])}: {html.escape(g['rejection_reason'] or 'no failure reason recorded')}"
                for g in failed_goals[:5]
            )
            failed_line = f"Failed (24h): {len(failed_goals)}{failed_parts}"
        else:
            failed_line = "Failed (24h): none"

        # Flag calibration (spec §4.4) — per-source:check raised/false_positive
        # counts over the last 30 days. Only false_positive > 0 checks get a
        # bullet -- that's the only actionable signal (a check with 0 false
        # positives fired correctly, nothing to calibrate) -- sorted
        # worst-offender-first; everything else rolls into the summary
        # counts. A dump of every check, mostly "0 false_positive", was an
        # unreadable wall of noise burying the rest of the digest.
        #
        # A bulleted vault_signals check gets its raw slug run through
        # _humanize_vault_check instead of shown verbatim -- that source's
        # `check` is a machine fingerprint (see relay_vault_signals.py), not
        # a label, and unlike collapsing it into a category bucket (tried
        # and reverted -- it laundered away exactly which finding was wrong,
        # and silently changed the total check count), this keeps the
        # per-finding identity while dropping only the parts a human never
        # needed to read.
        #
        # calibration is {} on an empty table or on gather's own exception
        # fallback above; both degrade to "none".
        if calibration:
            entries = []
            for key, counts in calibration.items():
                if not isinstance(counts, dict):
                    continue
                source, _, check = key.partition(":")
                label = _humanize_vault_check(check) if source == "vault_signals" else key
                entries.append((label, sum(counts.values()), counts.get("false_positive", 0)))

            total_raised = sum(e[1] for e in entries)
            total_fp = sum(e[2] for e in entries)
            calibration_line = (
                f"Flag calibration (30d): {len(entries)} checks, {total_raised} raised, "
                f"{total_fp} false positive{'s' if total_fp != 1 else ''}"
            )
            flagged = sorted((e for e in entries if e[2] > 0), key=lambda e: e[2], reverse=True)
            if flagged:
                calibration_line += "\n    - " + "\n    - ".join(
                    f"{label} — {raised} raised, {fp} false_positive" for label, raised, fp in flagged
                )
        else:
            calibration_line = "Flag calibration (30d): none"

        # Spec §3.6 addendum — count of currently auto-suppressed rules,
        # sourced from calibration.hint_report(30)'s "suppressed" group
        # (status=="active" CalibrationHint rows). hint_report is None on a
        # gather exception or non-dict result; both degrade to no suffix, same
        # as an empty/absent "suppressed" list. Append-only — never touches
        # calibration_line's existing text above.
        suppressed_hints = hint_report.get("suppressed") if isinstance(hint_report, dict) else None
        suppressed_count = len(suppressed_hints) if isinstance(suppressed_hints, list) else 0
        if suppressed_count > 0:
            def _fmt_suppressed(h):
                fp = h.get("fp_rate")
                if isinstance(fp, (int, float)):
                    return f"{h.get('fingerprint', '?')} (fp {fp:.0%})"
                return str(h.get("fingerprint", "?"))

            _named = [h for h in suppressed_hints if isinstance(h, dict)][:3]
            names = ", ".join(_fmt_suppressed(h) for h in _named)
            extra = f" (+{suppressed_count - 3} more)" if suppressed_count > 3 else ""
            calibration_line += f" | Auto-suppressed: {suppressed_count} rule(s): {names}{extra}"

        # Proposer drop visibility (B9) — what the goal proposer evaluated and
        # silently dropped over the last 24h, so a quiet proposer reads as
        # "nothing qualified" instead of "nothing ran". Filtered titles are
        # LLM-generated free text and must be escaped for Telegram's HTML
        # parse mode (matches how the digest is delivered — see notify_phone's
        # parse_mode="HTML" — though note the pre-existing goal-title lines
        # elsewhere in this function are NOT escaped; this block doesn't rely
        # on any such precedent, it escapes because it must).
        # `proposed` counts only goals still awaiting approval — an
        # auto-approved goal's entry["status"] is reassigned to
        # "auto_approved" in proposer.py, so it's excluded from `proposed`
        # and must be rendered separately or a fully-auto-approved tick would
        # misread as "did nothing".
        if proposer_stats:
            reason_parts = ", ".join(
                f"{reason}: {count}"
                for reason, count in proposer_stats["filtered_by_reason"].items()
            )
            proposer_line = (
                f"Proposer (24h): {proposer_stats['ticks']} tick(s), "
                f"{proposer_stats['proposed']} proposed, "
                f"{proposer_stats['auto_approved']} auto-approved, "
                f"{proposer_stats['filtered_total']} filtered"
            )
            if reason_parts:
                proposer_line += f" ({html.escape(reason_parts)})"
            if proposer_stats["filtered_items"]:
                proposer_line += "\n    - " + "\n    - ".join(
                    f"{html.escape(str(it.get('title') or '(untitled)'))}: {html.escape(str(it.get('reason') or 'unknown'))}"
                    for it in proposer_stats["filtered_items"]
                )
        else:
            proposer_line = "Proposer (24h): no ticks"

        lines = [
            f"NEXUS autonomy digest — {date_str}",
            f"Autonomy: {autonomy_label}",
            f"Auto-ran (24h): {auto_ran_line}",
            completed_line,
            failed_line,
            f"Awaiting your approval: {pending_count} action(s) + {len(proposed_goals)} proposed goal(s)",
            f"  proposed:{proposed_titles}",
            proposer_line,
            f"Spend today: ${spend:.2f} / ${daily_cap:.2f}",
            calibration_line,
        ]
        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"build_autonomy_digest failed (best-effort): {e}")
        return f"NEXUS autonomy digest — {datetime.utcnow().strftime('%Y-%m-%d')}\n(digest unavailable: {e})"


async def send_autonomy_digest() -> dict:
    """Build and deliver the daily autonomy digest via phone notification.

    Never raises. Returns {"delivered": bool, "text": str}.
    """
    try:
        from backend import events
        text = await build_autonomy_digest()
        delivered = await events.notify_phone(text, kind="autonomy_digest")
        return {"delivered": delivered, "text": text}
    except Exception as e:
        logger.debug(f"send_autonomy_digest failed (best-effort): {e}")
        return {"delivered": False, "text": ""}


async def send_spend_report() -> dict:
    """Build and deliver the weekly spend reconciliation report via phone notification.

    Surfaces NEXUS's metered spend per model over the last 7 days so Brian can
    compare against his actual Anthropic billing. Best-effort: never raises.
    Returns {"delivered": bool, "text": str}.
    """
    try:
        from backend.safety import governor
        from backend import events

        rep = await asyncio.to_thread(governor.spend_report, 7)

        verified_label = "VERIFIED" if rep.get("prices_verified") else "UNVERIFIED — compare to your Anthropic billing"
        lines = [
            f"NEXUS weekly spend (7d): ${rep['total_usd']:.2f} over {rep['total_calls']} calls",
        ]
        for entry in rep.get("by_model", []):
            lines.append(f"- {entry['model']}: ${entry['cost_usd']:.2f} ({entry['calls']} calls)")
        lines.append(f"Prices {verified_label}.")

        text = "\n".join(lines)
        delivered = await events.notify_phone(text, kind="spend_report")
        return {"delivered": delivered, "text": text}
    except Exception as e:
        logger.debug(f"send_spend_report failed (best-effort): {e}")
        return {"delivered": False, "text": ""}
