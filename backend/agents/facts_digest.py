"""Weekly Facts -> Brain digest (read/write/reference integration, piece 3).

Composes ONE markdown note per run summarizing facts that are new, changed, or
reinforced since the last run, and posts it into Brain/raw/ via the existing
obsidian.write_facts_digest -> POST /raw path (never Brain/wiki/ directly --
the nightly brain_organizer job at 02:00 is the sole raw->wiki consumer).

Watermark (SystemState.last_facts_digest_at) follows the same DB-backed
cooldown idiom as backend/agents/watchdog.py's dead-letter alert: a small sync
helper opens its own Session, reads/writes row 1, fails open/quiet on error.

Best-effort throughout except the write step itself: build_facts_digest()
never raises (mirrors digest.py's build_autonomy_digest), but
obsidian.write_facts_digest() DOES propagate failures (modeled on create_note,
not emit_event) so run_facts_digest() can decide whether to advance the
watermark -- a failed write must never be treated as "already digested".
"""
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SYNC DB helpers — call only via asyncio.to_thread
# ---------------------------------------------------------------------------

def _db_get_last_digest_at() -> datetime | None:
    """Read SystemState.last_facts_digest_at. Fail-quiet: returns None on any
    error (treated as "digest everything above-floor", bounded by the floor
    filter anyway)."""
    try:
        from sqlmodel import Session
        from backend.database import SystemState, engine

        with Session(engine) as session:
            row = session.get(SystemState, 1)
            return row.last_facts_digest_at if row else None
    except Exception as exc:
        logger.warning(f"_db_get_last_digest_at error (ignored): {exc}")
        return None


def _db_set_last_digest_at(ts: datetime) -> bool:
    """Write SystemState.last_facts_digest_at. Fail-quiet: returns False on
    any error instead of raising -- the caller logs an ERROR when this
    happens, since a failed watermark write means the next run will
    re-select and re-digest the same facts (safe, but worth surfacing)."""
    try:
        from sqlmodel import Session
        from backend.database import SystemState, engine

        with Session(engine) as session:
            row = session.get(SystemState, 1)
            if row is None:
                row = SystemState(id=1)
            row.last_facts_digest_at = ts
            session.add(row)
            session.commit()
        return True
    except Exception as exc:
        logger.warning(f"_db_set_last_digest_at error (ignored): {exc}")
        return False


# ---------------------------------------------------------------------------
# Pure formatter (no I/O)
# ---------------------------------------------------------------------------

def _format_digest(facts: list[dict], since: datetime | None) -> str:
    """Pure formatter, no I/O. Groups by subject, one bullet per fact."""
    now = datetime.utcnow()
    lines = [f"# NEXUS Facts Digest — {now.strftime('%Y-%m-%d')}", ""]
    if since is not None:
        lines.append(f"Facts new/changed/reinforced since {since.strftime('%Y-%m-%d %H:%M UTC')}.")
    else:
        lines.append("First digest — all currently-known facts above the confidence floor.")
    lines.append("")

    by_subject: dict[str, list[dict]] = {}
    for f in facts:
        by_subject.setdefault(f["subject"], []).append(f)

    for subject in sorted(by_subject):
        lines.append(f"## {subject}")
        for f in by_subject[subject]:
            lines.append(f"- {f['predicate']}: {f['value']} (confidence {f['effective_confidence']:.2f})")
        lines.append("")

    lines.append("Powered by CwiAI")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------

async def build_facts_digest() -> tuple[str, list[dict]]:
    """Compute the digest text + the list of facts it covers.

    Returns ("", []) when nothing qualifies (no watermark-eligible facts
    above EFFECTIVE_FLOOR) -- the caller treats that as "skip the write,
    a zero-change note is noise." Never raises (mirrors digest.py's
    build_autonomy_digest); any failure degrades to ("", []).
    """
    try:
        from backend.agents import facts as facts_mod

        since = await asyncio.to_thread(_db_get_last_digest_at)
        all_facts = await facts_mod.list_facts_for_audit()

        selected = []
        for f in all_facts:
            if not f.get("above_floor"):
                continue
            last_seen_raw = f.get("last_seen_at") or f.get("created_at")
            if not last_seen_raw:
                continue
            last_seen_dt = datetime.fromisoformat(last_seen_raw)
            if since is not None and last_seen_dt <= since:
                continue
            selected.append(f)

        if not selected:
            return "", []

        return _format_digest(selected, since), selected
    except Exception as exc:
        logger.warning(f"build_facts_digest failed (ignored): {exc}")
        return "", []


async def run_facts_digest() -> dict:
    """Build the digest and, if non-empty, write it to Brain + advance the
    watermark. Never raises. Returns
    {"written": bool, "count": int, "watermark_advanced": bool}.

    The watermark only advances on a CONFIRMED write -- a failed
    obsidian.write_facts_digest (which propagates errors, unlike the
    fire-and-forget emit_event) leaves it untouched so the same facts are
    reconsidered next run instead of being silently treated as delivered.

    run_started is captured ONCE, before the DB read, and is the same value
    used as the new watermark -- a fact reinforced during the write's network
    round-trip (bounded by _post_raw's timeout) would otherwise get a
    last_seen_at earlier than a post-write timestamp and be skipped forever.
    """
    run_started = datetime.utcnow()
    text, selected = await build_facts_digest()
    if not text:
        logger.info("facts digest: nothing new since last run, skipping write")
        return {"written": False, "count": 0, "watermark_advanced": False}

    try:
        from backend.integrations import obsidian
        await obsidian.write_facts_digest(text)
    except Exception as exc:
        logger.warning(f"facts digest: write failed, watermark not advanced: {exc}")
        return {"written": False, "count": 0, "watermark_advanced": False}

    advanced = await asyncio.to_thread(_db_set_last_digest_at, run_started)
    if not advanced:
        logger.error(
            "facts digest: note WRITTEN but watermark did not advance -- "
            "next run will re-digest the same facts"
        )
    return {"written": True, "count": len(selected), "watermark_advanced": advanced}
