"""Is the graph still being refreshed? (G3a follow-up, 2026-11-03)

A failing collect or vault index only logs a warning, so what_changed and the incident evidence block would
keep answering "none recorded" from data that stopped updating. This turns "no successful run for STALE_RUNS
intervals" into (a) a low-severity `graph:stale:<job>` flag that closes itself on recovery and (b) a warning
line on every answer built from the graph.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

STALE_RUNS = 3
# job name in graphjob -> which interval it runs on
WATCHED = {
    "collect": "collect",
    "vault_index": "vault_index",
    "source:proxmox_api": "collect",
    "source:unraid_api": "collect",
    "source:uptime_targets": "collect",
}


def stale(jobs: dict[str, dict], minutes: dict[str, int], now: datetime) -> dict[str, dict]:
    """{job: {"last_success", "last_error"}} for watched jobs with no success in STALE_RUNS intervals.
    A job with no graphjob row at all is not judged (graph just enabled, or that job never registered)."""
    out: dict[str, dict] = {}
    for name, kind in WATCHED.items():
        j = jobs.get(name)
        if not j:
            continue
        last = j.get("last_success")
        if last is None:
            if j.get("last_error"):
                out[name] = {"last_success": None, "last_error": j["last_error"]}
            continue
        if now - datetime.fromisoformat(last) > timedelta(minutes=STALE_RUNS * minutes[kind]):
            out[name] = {"last_success": last, "last_error": j.get("last_error")}
    return out


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def current() -> dict[str, dict]:
    from backend.graph import intervals, store

    return stale(store.jobs(), intervals(), _now())


def note() -> str | None:
    """One warning line for answers built from the graph, or None when every watched job is fresh. Never raises."""
    try:
        st = current()
    except Exception as e:
        return f"⚠ graph freshness unknown ({e}) — recent changes may be missing"
    if not st:
        return None
    parts = [f"{n} last succeeded {v['last_success'] + 'Z' if v['last_success'] else 'never'}" for n, v in sorted(st.items())]
    return "⚠ graph data may be incomplete (" + "; ".join(parts) + ") — recent changes may be missing"


async def update_flags() -> None:
    """Open/clear graph:stale:<job> flags. Called after every graph job run. Never raises."""
    try:
        from backend.agents import outcomes

        st = await asyncio.to_thread(current)
        for name in WATCHED:
            if name in st:
                v = st[name]
                since = f"since {v['last_success']}Z" if v["last_success"] else "since it was enabled"
                err = f" Last error: {v['last_error']}" if v.get("last_error") else ""
                summary = (f"Knowledge graph: {name} has not succeeded {since} ({STALE_RUNS}+ runs); what_changed "
                           f"and alert evidence may miss changes. Run `python -m backend.graph doctor`.{err}")[:300]
                await outcomes.record_flag_ex("graph", f"stale:{name}", summary, severity="low")
            else:
                await outcomes.clear_flag("graph", f"stale:{name}")
    except Exception as e:
        logger.warning(f"graph freshness flags failed (ignored): {e}")
