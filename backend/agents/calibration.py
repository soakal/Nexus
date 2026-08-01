"""Calibration Loop (docs/calibration-loop-spec.md) — the nightly aggregation
job (§2): reads OutcomeFlag and writes CalibrationHint.

Split per §2.1 to avoid a circular import: the READ-SIDE accessors the hot
path needs (`active_hint`, `should_page`, `record_flag_ex`, plus
`record_flag`'s back-compat wrapper) live in `backend.agents.outcomes` (landed
the previous cycle). This module imports `outcomes`'s table (`OutcomeFlag`)
and its own new table (`CalibrationHint`) directly from `backend.database`;
`outcomes.py` never imports this module.

Same discipline as outcomes.py/digest.py/facts_cleanup.py: sync `_db_*`
helpers open their own Session (re-importing `engine` from backend.database
on every call so tests that monkeypatch backend.database.engine are
honoured), called exclusively via asyncio.to_thread from the public async
API — no Session/ORM object ever crosses an await boundary (CAL48). This
module never imports backend.safety.broker or the websocket layer (CAL47).
Zero LLM calls (CAL49).

This cycle (rollout §9.5 step 2) ships only `recompute_hints()` — the §2.3
honest-denominator aggregation plus the persisted §2.4/§2.5 state machine.
Nothing pages differently as a result: `calibration_suppression_enabled`
still defaults False (landed previous cycle in outcomes.should_page), so
hints compute and persist but the gate stays inert. Scheduler registration
(§2.2, CAL45/CAL46) and `/calibration` + `hint_report`/`set_override`
(§4/§5) are future cycles.
"""
import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# low < medium < high, matching ActionLog.risk / Goal.risk / OutcomeFlag.severity.
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}

_ZEROED_RESULT = {
    "scanned": 0, "activated": [], "expired": [], "unchanged": 0, "skipped_override": [],
}


def _bucket_for(dt: datetime, tz) -> str:
    """Coarse 4-bucket time-of-day classification in `tz` (§1.4) — computed
    for display in `/calibration`'s reason string only, never read by the
    suppression gate."""
    from zoneinfo import ZoneInfo
    local = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    hour = local.hour
    if hour < 6:
        return "night"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    return "evening"


def _build_reason(
    *, verdict_count: int, false_positive_count: int, fp_rate: float, window_days: int,
    bucket_counts: dict[str, tuple[int, int]], suppressed_surfacings: int,
    first_active_at: datetime | None, is_active: bool,
) -> str:
    """Plain-sentence reason (§2.4's example), rendered verbatim by a future
    /calibration. Empty string when there is nothing judged yet to report."""
    if verdict_count == 0:
        return ""
    pct = round(fp_rate * 100)
    bucket_str = ", ".join(
        f"{name} {bucket_counts.get(name, (0, 0))[0]}/{bucket_counts.get(name, (0, 0))[1]}"
        for name in ("night", "morning", "afternoon", "evening")
        if name in bucket_counts
    )
    sentence = (
        f"{false_positive_count} of {verdict_count} judged flags were false alarms "
        f"({pct}%) in the last {window_days} days."
    )
    if bucket_str:
        sentence += f" By time of day: {bucket_str}."
    if is_active and suppressed_surfacings and first_active_at:
        plural = "s" if suppressed_surfacings != 1 else ""
        sentence += (
            f" {suppressed_surfacings} further occurrence{plural} suppressed since "
            f"{first_active_at.strftime('%Y-%m-%d')}."
        )
    return sentence


# ---------------------------------------------------------------------------
# Sync DB helper — called exclusively via asyncio.to_thread.
# ---------------------------------------------------------------------------

def _db_recompute(now: datetime, settings) -> dict:
    """The whole nightly job in one Session: scan the trailing window, group
    by fingerprint, compute the honest denominator (§2.3), run the persisted
    state machine (§2.4/§2.5) per fingerprint, and apply the un-suppression
    side effect (clear `suppressed` on any open OutcomeFlag row for a
    fingerprint that just transitioned out of `active`, §2.5).

    Returns the §2.6 shape plus an internal-only "activated_details" list
    (popped by the async wrapper before it fires notify_phone) — kept out of
    this function so the notify call, which is async, never has to happen on
    a thread.
    """
    from sqlmodel import Session, select
    from backend.database import CalibrationHint, OutcomeFlag, engine

    window_days = int(getattr(settings, "calibration_window_days", 30))
    min_verdicts = int(getattr(settings, "calibration_min_verdicts", 5))
    fp_threshold = float(getattr(settings, "calibration_fp_threshold", 0.60))
    clear_threshold = float(getattr(settings, "calibration_clear_threshold", 0.40))
    hint_max_days = int(getattr(settings, "calibration_hint_max_days", 30))
    calibration_enabled = bool(getattr(settings, "calibration_enabled", True))

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(getattr(settings, "briefing_timezone", "UTC") or "UTC")
    except Exception:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")

    cutoff = now - timedelta(days=window_days)

    with Session(engine) as session:
        rows = session.exec(
            select(OutcomeFlag).where(OutcomeFlag.created_at >= cutoff)
        ).all()

        # §6/CAL10 — a "missed detection" row has no rule to calibrate;
        # excluded from the scan entirely, not merely from the denominator.
        rows = [
            r for r in rows
            if not (r.source == "manual" and (r.check or "").startswith("missed:"))
        ]

        by_fp: dict[str, list] = {}
        for r in rows:
            by_fp.setdefault(r.fingerprint, []).append(r)

        existing_hints = {h.fingerprint: h for h in session.exec(select(CalibrationHint)).all()}

        scanned = 0
        activated: list[str] = []
        activated_details: list[dict] = []
        expired: list[str] = []
        skipped_override: list[str] = []
        unchanged = 0

        for fingerprint, frows in by_fp.items():
            scanned += 1
            existing = existing_hints.get(fingerprint)

            # §5's stickiness (checked here defensively; should_page's own
            # override_until check is the primary enforcement point) — a
            # recompute must never silently undo Brian's override.
            if existing is not None and existing.override_until is not None and existing.override_until > now:
                skipped_override.append(fingerprint)
                continue

            # §2.3 — the honest denominator. suppressed=True rows never
            # reached Brian (no verdict); they contribute to
            # suppressed_surfacings only.
            suppressed_rows = [r for r in frows if r.suppressed]
            live_rows = [r for r in frows if not r.suppressed]
            suppressed_surfacings = sum(r.surfaced_count for r in suppressed_rows)

            verdict_rows = [
                r for r in live_rows
                if r.status in ("resolved", "false_positive")
                and not (r.resolved_by or "").startswith("auto:")
            ]
            auto_cleared_count = sum(1 for r in live_rows if (r.resolved_by or "").startswith("auto:"))
            false_positive_count = sum(1 for r in verdict_rows if r.status == "false_positive")
            verdict_count = len(verdict_rows)
            fp_rate = (false_positive_count / verdict_count) if verdict_count else 0.0

            max_severity = "medium"
            best_rank = -1
            for r in live_rows:
                rank = _SEVERITY_RANK.get(r.severity, 1)
                if rank > best_rank:
                    best_rank = rank
                    max_severity = r.severity

            bucket_counts: dict[str, list[int]] = {}
            for r in verdict_rows:
                counts = bucket_counts.setdefault(_bucket_for(r.created_at, tz), [0, 0])
                counts[1] += 1
                if r.status == "false_positive":
                    counts[0] += 1
            bucket_counts_t = {k: (v[0], v[1]) for k, v in bucket_counts.items()}

            was_active = existing is not None and existing.status == "active"
            should_activate = (
                calibration_enabled
                and verdict_count >= min_verdicts
                and fp_rate >= fp_threshold
            )

            if was_active:
                # §2.5(b) — mandatory re-probation, unconditional, ahead of
                # the rate. §2.5(a) — hysteresis: only the LOW clear
                # threshold drops an active hint; the gap between it and the
                # suppress threshold prevents daily flapping.
                expire = (
                    (existing.expires_at is not None and existing.expires_at <= now)
                    or fp_rate < clear_threshold
                )
                new_status = "expired" if expire else "active"
            else:
                new_status = "active" if should_activate else "expired"

            transitioned_to_active = new_status == "active" and not was_active
            transitioned_out_of_active = was_active and new_status != "active"

            hint = existing if existing is not None else CalibrationHint(fingerprint=fingerprint)
            hint.context_bucket = "all"
            hint.status = new_status
            hint.verdict_count = verdict_count
            hint.false_positive_count = false_positive_count
            hint.fp_rate = fp_rate
            hint.auto_cleared_count = auto_cleared_count
            hint.suppressed_surfacings = suppressed_surfacings
            hint.max_severity = max_severity
            hint.window_days = window_days
            hint.computed_at = now
            hint.updated_at = now

            if transitioned_to_active:
                # first_active_at is NEVER reset by a recompute of an
                # already-active hint — only a genuine (re-)activation event
                # sets it fresh, even if the hint was active before and has
                # since expired once.
                hint.first_active_at = now
                hint.expires_at = now + timedelta(days=hint_max_days)
                hint.override_by = None
                hint.override_at = None
                hint.override_until = None
                hint.override_note = None

            hint.reason = _build_reason(
                verdict_count=verdict_count,
                false_positive_count=false_positive_count,
                fp_rate=fp_rate,
                window_days=window_days,
                bucket_counts=bucket_counts_t,
                suppressed_surfacings=suppressed_surfacings,
                first_active_at=hint.first_active_at,
                is_active=(new_status == "active"),
            )

            session.add(hint)

            if transitioned_to_active:
                activated.append(fingerprint)
                activated_details.append({
                    "fingerprint": fingerprint,
                    "fp_rate": fp_rate,
                    "verdict_count": verdict_count,
                })
            elif transitioned_out_of_active:
                expired.append(fingerprint)
            else:
                unchanged += 1

        if by_fp:
            session.commit()

        # §2.5's side effect: on any transition OUT of active (hysteresis,
        # re-probation), clear suppressed/suppressed_reason on any
        # currently-open OutcomeFlag row for that fingerprint — otherwise a
        # rule that un-suppresses while its condition is still live stays
        # invisible until it happens to re-fire (CAL18).
        if expired:
            open_rows = session.exec(
                select(OutcomeFlag)
                .where(OutcomeFlag.fingerprint.in_(expired))
                .where(OutcomeFlag.status == "open")
            ).all()
            for row in open_rows:
                row.suppressed = False
                row.suppressed_reason = None
                row.updated_at = now
                session.add(row)
            if open_rows:
                session.commit()

        return {
            "scanned": scanned,
            "activated": activated,
            "activated_details": activated_details,
            "expired": expired,
            "unchanged": unchanged,
            "skipped_override": skipped_override,
        }


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def recompute_hints() -> dict:
    """Nightly aggregation job entry point (spec §2, rollout §9.5 step 2).

    For every fingerprint with OutcomeFlag activity inside the trailing
    `calibration_window_days` window: computes the §2.3 honest-denominator
    FP rate and runs the persisted §2.4/§2.5 state machine (activate,
    hysteresis-clear, mandatory re-probation expiry, override stickiness),
    then fires one `events.notify_phone(kind="calibration_suppress")` per
    newly-activated hint (§2.6). Expiry is never paged — going back to
    normal alerting is self-announcing.

    Returns {"scanned": int, "activated": [fp], "expired": [fp],
    "unchanged": int, "skipped_override": [fp]}.

    NEVER raises — a calibration bug must never take out the 03:45-03:50
    nightly hygiene block. Any exception is logged and a zeroed dict is
    returned (mirrors record_flag's contract, CAL12).
    """
    try:
        from backend.config import get_settings
        settings = get_settings()
        now = datetime.utcnow()

        result = await asyncio.to_thread(_db_recompute, now, settings)
        activated_details = result.pop("activated_details", [])

        if activated_details:
            from backend import events
            for detail in activated_details:
                pct = round(detail["fp_rate"] * 100)
                message = (
                    f"Calibration: auto-suppressing {detail['fingerprint']} "
                    f"({pct}% false alarm, {detail['verdict_count']} judged flags)."
                )
                await events.notify_phone(message, kind="calibration_suppress")

        logger.info(
            f"Calibration recompute: scanned={result['scanned']} "
            f"activated={result['activated']} expired={result['expired']} "
            f"unchanged={result['unchanged']} skipped_override={result['skipped_override']}"
        )
        return result
    except Exception as e:
        logger.error(f"recompute_hints failed: {e}")
        return dict(_ZEROED_RESULT)
