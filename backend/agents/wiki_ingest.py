"""Wiki fragmentation audit — the read-only survivor of the old raw->wiki
ingestion watcher.

The watcher itself (ingest_file/run_all_unprocessed/_import_reference_doc and
the watchdog-based start/stop_wiki_watcher) was deleted 2026-08-14 (linux-lxc
fix-plan Phase 5.1) after brain_organizer.py became the sole raw->wiki
pipeline (2026-07-14) made it permanently dead code -- confirmed zero
remaining callers before deletion. weekly_fragmentation_report is a
separate, still-live Sunday scheduler job with no relation to that pipeline:
a read-only scan for clusters of small same-prefix wiki pages, never
merges/deletes anything.
"""

import asyncio
import logging
import re
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

# A raw filename that IS a bare date (e.g. "2026-07-01", optional trailing
# letter like session files use), or names itself a briefing/daily log.
_DAILY_NOTE_STEM_PAT = re.compile(r"^\d{4}-\d{2}-\d{2}[a-z]?$", re.IGNORECASE)
_DAILY_NOTE_NAME_PAT = re.compile(r"briefing|daily", re.IGNORECASE)
_DATE_IN_STEM_PAT = re.compile(r"\d{4}-\d{2}-\d{2}")
_EVENT_NOTE_PREFIX_PAT = re.compile(r"^event-[a-z0-9]+(?:-[a-z0-9]+)*?-", re.IGNORECASE)


def _is_daily_note(stem: str) -> bool:
    """True for a morning-briefing/daily-log filename (mirrors
    modules/brain-organizer/brain_organizer.py::_is_daily_note).

    These are heavy with '##' headers (Weather, System Health, Priority
    Actions...) and so trip a reference-doc heuristic -- without this guard
    they'd get imported VERBATIM as a new wiki page named after the date,
    fragmenting the wiki with one page per day instead of being distilled
    into the relevant topic pages like any other session note.

    A pure date stem always counts. A "daily"/"briefing"-named file only
    counts if it ALSO contains a date or a generalized event-source prefix
    ("event-<source>-") -- otherwise a genuinely topical file that happens to
    have "daily" in its name (e.g. "Daily-Driver-Setup.md") would get
    hijacked into the daily-note path.
    """
    if _DAILY_NOTE_STEM_PAT.match(stem):
        return True
    if _DAILY_NOTE_NAME_PAT.search(stem):
        return bool(_DATE_IN_STEM_PAT.search(stem)) or bool(_EVENT_NOTE_PREFIX_PAT.match(stem))
    return False


_CLUSTER_TAIL = re.compile(r"[-_ ]?(?:pass)?\d+[a-z]?$", re.IGNORECASE)


def _fragmentation_report_sync(vault: Path) -> list[str]:
    """Read-only: group wiki pages by shared prefix; flag clusters of >=5 small files.

    Never merges or deletes — returns human-readable warning lines only.
    """
    wiki_dir = vault / "Brain" / "wiki"
    if not wiki_dir.exists():
        return []
    clusters: dict[str, list[str]] = {}
    for f in wiki_dir.glob("*.md"):
        if f.parent.name == "processed":
            continue
        # cluster key = stem with trailing number/pass token stripped
        prefix = _CLUSTER_TAIL.sub("", f.stem).rstrip("-_ ")
        if not prefix:
            continue
        # only count small files (stubs) as fragmentation candidates
        try:
            if f.stat().st_size <= 4096:
                clusters.setdefault(prefix, []).append(f.name)
        except Exception:
            continue
    return [
        f"Possible fragmentation: {prefix}-* ({len(names)} small files) — review for consolidation."
        for prefix, names in sorted(clusters.items())
        if len(names) >= 5
    ]


async def weekly_fragmentation_report() -> dict:
    """Post a fragmentation warning as a raw note if any cluster of >=5 stubs
    exists. Read-only audit — never merges/deletes. Called weekly by the
    scheduler.

    Goes through obsidian.write_fragmentation_report() (POST :8765/raw), NOT
    a direct pathlib write to Inbox.md -- this used to bypass the MCP write
    surface every other writer in this codebase goes through (the one
    confirmed direct-vault-write inconsistency from the migration's
    Windows-code audit). Behavior change: the report now arrives via the
    raw->wiki digestion pipeline like every other note, appended to Inbox.md
    by the next brain_organizer run, rather than appearing in Inbox.md
    immediately/synchronously.
    """
    try:
        from backend.config import get_settings
        vault = Path(get_settings().obsidian_vault_path)
        lines = await asyncio.to_thread(_fragmentation_report_sync, vault)
        if not lines:
            return {"clusters": 0}
        today = date.today().isoformat()
        section = f"## {today} — Fragmentation report\n" + "\n".join(f"- {ln}" for ln in lines) + "\n"
        from backend.integrations import obsidian
        await obsidian.write_fragmentation_report(section)
        logger.info(f"wiki fragmentation report: {len(lines)} clusters flagged")
        return {"clusters": len(lines)}
    except Exception as e:
        logger.warning(f"fragmentation report error: {e}")
        return {"error": str(e)}
