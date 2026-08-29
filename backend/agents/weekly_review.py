"""Weekly self-review — a Sunday-evening memo over NEXUS's own real 7-day
aggregates (ActionLog, OutcomeFlag, spend), with at most 3 tappable "mute
this noisy kind" buttons.

Deliberately NOT a `setting_change` broker kind: thresholds like
homelab_disk_temp_warn_c live in pydantic Settings (backend/config.py),
sourced from .env and cached by get_settings() at process start -- they are
NOT runtime-mutable today. A tappable threshold change would need
.env-write+restart machinery or migrating fields into SystemState, neither
justified for v1. The one lever that IS already runtime-mutable and safe to
offer is a per-kind notify mute (SystemState.muted_notify_kinds via
governor.add_muted_notify_kind) -- same mechanism Telegram's own /mute
command already uses.

Mute-button candidates are built DETERMINISTICALLY from the aggregated data,
never parsed out of the LLM's memo text -- same discipline as briefing.py's
_UNVERIFIED_FACT_SECTIONS convention (the model narrates, it doesn't decide
what's tappable).
"""

import asyncio
import html
import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Maps a homelab_watch OutcomeFlag fingerprint (source:check) to the notify
# kind Telegram's /mute already understands. vm:/docker: are prefixes since
# those checks embed a per-instance id/name; the other four are exact.
_FINGERPRINT_PREFIX_KIND = {
    "homelab_watch:vm:": "homelab_vm_stopped",
    "homelab_watch:docker:": "homelab_docker_stopped",
    "homelab_watch:unraid_array": "homelab_array",
    "homelab_watch:unraid_temp": "homelab_disk_temp",
    "homelab_watch:garage_open": "homelab_garage",
    "homelab_watch:vzdump_failed": "homelab_backup_failed",
}
_MUTE_SURFACED_THRESHOLD = 10
_MUTE_MAX_CANDIDATES = 3

_SYSTEM = (
    "You are NEXUS writing your own weekly self-review for Brian. Below are "
    "your real aggregates for the last 7 days (JSON). Write a memo under 180 "
    "words: (1) what you did (actions, spend, top cost labels); (2) what "
    "went wrong or was noisy (failed actions, false-positive flags, repeat "
    "alerts); (3) at most 3 concrete recommendations, each naming an exact "
    "existing lever: /mute <kind>, /defer <id> <days>, /resolve <id>, or a "
    "budget change. Only reference kinds/fingerprints/labels present in the "
    "data — never invent one. Plain text, no markdown headers."
)


def _kind_for_fingerprint(fp: str) -> str | None:
    for prefix, kind in _FINGERPRINT_PREFIX_KIND.items():
        if fp.startswith(prefix):
            return kind
    return None


def _db_action_log_counts(cutoff: datetime) -> list[dict]:
    from sqlmodel import Session, func, select
    from backend.database import ActionLog, engine
    with Session(engine) as session:
        stmt = (
            select(ActionLog.kind, ActionLog.decision, func.count())
            .where(ActionLog.created_at >= cutoff)  # type: ignore[attr-defined]
            .group_by(ActionLog.kind, ActionLog.decision)
        )
        rows = session.exec(stmt).all()
        return [{"kind": k, "decision": d, "count": c} for k, d, c in rows]


def _db_mute_candidates(cutoff: datetime, already_muted: set[str]) -> list[dict]:
    """Per-notify-kind {surfaced, false_positive_flags} for homelab_watch
    flags surfaced in the window, filtered to kinds crossing the noise
    threshold, not already muted, sorted by surfaced count descending."""
    from sqlmodel import Session, select
    from backend.database import OutcomeFlag, engine
    with Session(engine) as session:
        stmt = (
            select(OutcomeFlag)
            .where(OutcomeFlag.source == "homelab_watch")
            .where(OutcomeFlag.last_surfaced_at >= cutoff)  # type: ignore[attr-defined]
        )
        rows = session.exec(stmt).all()

    by_kind: dict[str, dict] = {}
    for row in rows:
        kind = _kind_for_fingerprint(row.fingerprint)
        if kind is None or kind in already_muted:
            continue
        bucket = by_kind.setdefault(kind, {"kind": kind, "surfaced": 0, "false_positive": 0})
        bucket["surfaced"] += row.surfaced_count
        if row.status == "false_positive":
            bucket["false_positive"] += 1

    candidates = [
        b for b in by_kind.values()
        if b["surfaced"] >= _MUTE_SURFACED_THRESHOLD and b["false_positive"] >= 1
    ]
    candidates.sort(key=lambda b: b["surfaced"], reverse=True)
    return candidates[:_MUTE_MAX_CANDIDATES]


async def _gather(cutoff: datetime) -> dict:
    from backend.agents import outcomes
    from backend.safety import governor

    actions = await asyncio.to_thread(_db_action_log_counts, cutoff)
    muted = await asyncio.to_thread(governor.get_muted_notify_kinds)
    mute_candidates = await asyncio.to_thread(_db_mute_candidates, cutoff, muted)
    calibration = await outcomes.calibration_summary(days=7)
    spend = await asyncio.to_thread(governor.spend_report, 7)

    return {
        "actions": actions,
        "calibration": calibration,
        "spend": {
            "total_usd": spend.get("total_usd", 0.0),
            "total_calls": spend.get("total_calls", 0),
            "by_label": spend.get("by_label", [])[:5],
        },
        "mute_candidates": mute_candidates,
        "already_muted": sorted(muted),
    }


def _fallback_memo(data: dict) -> str:
    """Deterministic stats block used when the LLM call fails -- the memo
    still ships, just without narrative prose."""
    lines = [
        f"Spend: ${data['spend']['total_usd']:.2f} over {data['spend']['total_calls']} calls",
    ]
    for entry in data["spend"]["by_label"][:3]:
        lines.append(f"  - {entry['label']}: ${entry['cost_usd']:.2f} ({entry['calls']} calls)")
    by_decision: dict[str, int] = {}
    for a in data["actions"]:
        by_decision[a["decision"]] = by_decision.get(a["decision"], 0) + a["count"]
    if by_decision:
        lines.append("Actions: " + ", ".join(f"{d}={c}" for d, c in sorted(by_decision.items())))
    if data["mute_candidates"]:
        names = ", ".join(c["kind"] for c in data["mute_candidates"])
        lines.append(f"Noisy kinds worth muting: {names}")
    return "\n".join(lines)


def _mute_buttons(mute_candidates: list[dict]) -> list | None:
    if not mute_candidates:
        return None
    return [
        {"text": f"🔇 Mute {c['kind']}", "callback_data": f"mute:on:{c['kind']}"}
        for c in mute_candidates
    ]


async def run_weekly_review() -> dict:
    """Top-level entry point called by the scheduler weekly. Never raises —
    any exception is caught and logged. Returns a summary dict."""
    try:
        from backend.config import get_settings
        if not getattr(get_settings(), "weekly_review_enabled", True):
            return {"skipped": "disabled"}

        cutoff = datetime.utcnow() - timedelta(days=7)
        data = await _gather(cutoff)

        if not data["actions"] and not data["mute_candidates"] and data["spend"]["total_usd"] == 0:
            return {"skipped": "no_activity"}

        try:
            from backend.agents import router
            prompt = f"DATA:\n{json.dumps(data, default=str)}"
            # effort="low" (2026-08-28): routine weekly digest, no interactive
            # user waiting on this turn.
            memo = await router.sonnet(prompt, system=_SYSTEM, label="weekly_review", effort="low")
        except Exception as e:
            logger.warning(f"weekly_review: LLM call failed, using fallback memo: {e}")
            memo = _fallback_memo(data)

        from backend import events
        buttons = _mute_buttons(data["mute_candidates"])
        delivered = await events.notify_phone(
            f"📋 NEXUS weekly review\n{html.escape(memo)[:3500]}",
            kind="weekly_review",
            buttons=buttons,
        )
        return {"skipped": None, "delivered": delivered}
    except Exception as e:
        logger.error(f"run_weekly_review error (ignored): {e}")
        return {"skipped": "error"}
