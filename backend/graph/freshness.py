"""Is the graph still being refreshed? (G3a follow-up, 2026-11-03)

A failing collect or vault index only logs a warning, so what_changed and the incident evidence block would
keep answering "none recorded" from data that stopped updating. This turns "no successful run for STALE_RUNS
intervals" into (a) a low-severity `graph:stale:<job>` flag, reconciled by the 5-min watchdog (independent of
the graph jobs themselves), closed on recovery or when the graph is switched off, and (b) a warning line on
every answer built from the graph.
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


def stale(jobs: dict[str, dict], minutes: dict[str, int], now: datetime, started: datetime | None) -> dict[str, dict]:
    """{job: {"last_success", "last_error"}} for watched jobs with no success for STALE_RUNS intervals.

    The clock runs from the job's last success, or -- when it has never succeeded or has no graphjob row at
    all -- from `started` (when this process began running the graph jobs), so a failed first run after boot
    gets the same grace as any other. With no `started` (a process that does not run the jobs, e.g. the
    CLI) a job without a success is not judged."""
    out: dict[str, dict] = {}
    for name, kind in WATCHED.items():
        j = jobs.get(name) or {}
        last = j.get("last_success")
        since = datetime.fromisoformat(last) if last else started
        if since is None:
            continue
        if now - since >= timedelta(minutes=STALE_RUNS * minutes[kind]):
            out[name] = {"last_success": last, "last_error": j.get("last_error")}
    return out


_started: datetime | None = None


def mark_started(at: datetime | None = None) -> None:
    """Called once when the scheduler registers the graph jobs (process boot)."""
    global _started
    _started = at or _now()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def current() -> dict[str, dict]:
    from backend.graph import intervals, store

    return stale(store.jobs(), intervals(), _now(), _started)


def note() -> str | None:
    """One warning line for answers built from the graph, or None when every watched job is fresh. Never raises."""
    try:
        st = current()
    except Exception as e:
        return f"⚠ graph freshness unknown ({e}) — recent changes may be missing"
    if not st:
        return None
    parts = [f"{n} last succeeded {v['last_success'] + 'Z' if v['last_success'] else 'never since restart'}"
             for n, v in sorted(st.items())]
    return "⚠ graph data may be incomplete (" + "; ".join(parts) + ") — recent changes may be missing"


_RECONCILE_TIMEOUT_S = 30


async def reconcile() -> list[str]:
    """Watchdog check (independent of the graph jobs, so a hung or unscheduled job is still caught).
    Graph enabled: open/clear graph:stale:<job> flags and return the stale job names. Graph disabled:
    clear any left-over flags. Bounded; never raises."""
    try:
        return await asyncio.wait_for(_reconcile(), _RECONCILE_TIMEOUT_S)
    except Exception as e:  # incl. TimeoutError
        logger.warning(f"graph freshness check failed (ignored): {e!r}")
        return []


async def _reconcile() -> list[str]:
    from backend.agents import outcomes
    from backend.graph import enabled

    st = await asyncio.to_thread(current) if enabled() else {}
    for name in WATCHED:
        if name in st:
            v = st[name]
            since = f"since {v['last_success']}Z" if v["last_success"] else "since NEXUS restarted"
            err = f" Last error: {v['last_error']}" if v.get("last_error") else ""
            summary = (f"Knowledge graph: {name} has not succeeded {since} ({STALE_RUNS}+ runs); what_changed "
                       f"and alert evidence may miss changes. Run `python -m backend.graph doctor`.{err}")[:300]
            await outcomes.record_flag_ex("graph", f"stale:{name}", summary, severity="low")
        else:
            await outcomes.clear_flag("graph", f"stale:{name}")
    return sorted(st)
