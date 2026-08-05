"""Deterministic dashboard state collectors.

Collectors run from APScheduler, never from a browser request. Each refresh
group is sequential and APScheduler enforces max_instances=1, preventing a
slow/down integration from creating overlapping work.

Broadcasts go through `state_ws_manager` (backend/api/agents.py), a SEPARATE
WebSocketManager instance from the one `/ws/logs` uses — sharing a broadcaster
would leak `state.updated` JSON into the Safety/Traces log viewer (AgentLog.jsx
pushes every incoming message straight into its displayed log list with no
type filtering) and leak real log lines into `/ws/state` clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi.encoders import jsonable_encoder

from backend.state_store import store_failure, store_success

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Collector:
    key: str
    ttl_seconds: int
    load: Callable[[], Awaitable[object]]


async def _source_health(module_name: str) -> object:
    module = __import__(f"backend.integrations.{module_name}", fromlist=["health_check"])
    return {"healthy": bool(await module.health_check())}


async def _fetch(module_name: str) -> object:
    module = __import__(f"backend.integrations.{module_name}", fromlist=["fetch"])
    return await module.fetch()


async def _proxmox_maintenance() -> object:
    from backend.api.proxmox_api import get_proxmox_maintenance

    return await get_proxmox_maintenance(None)


async def _brain_status() -> object:
    from backend.api.brain_organizer import brain_organizer_status

    return await brain_organizer_status(None)


async def _mail_status() -> object:
    from backend.api.protonmail import _dashboard_inbox

    return await _dashboard_inbox()


async def _latest_briefing() -> object:
    from sqlmodel import Session, select

    from backend.database import Briefing, engine

    def _read():
        with Session(engine) as session:
            row = session.exec(select(Briefing).order_by(Briefing.created_at.desc()).limit(1)).first()
            return row or {"created_at": None}

    return await asyncio.to_thread(_read)


def _source(key: str, seconds: int) -> Collector:
    return Collector(key=f"source.{key}", ttl_seconds=seconds * 2, load=lambda key=key: _source_health(key))


COLLECTOR_GROUPS: dict[int, tuple[Collector, ...]] = {
    30: (
        _source("homeassistant", 30),
        _source("unifi", 30),
        _source("unraid", 30),
        _source("proxmox", 30),
        _source("adguard", 30),
        Collector("dashboard.adguard", 60, lambda: _fetch("adguard")),
        Collector("dashboard.unraid", 60, lambda: _fetch("unraid")),
        Collector("dashboard.proxmox", 60, lambda: _fetch("proxmox")),
    ),
    60: (
        _source("obsidian", 60),
        _source("hermes", 60),
        _source("channels_dvr", 60),
        Collector("dashboard.channels", 120, lambda: _fetch("channels_dvr")),
        Collector("dashboard.proxmox_maintenance", 120, _proxmox_maintenance),
        Collector("dashboard.brain", 120, _brain_status),
        Collector("dashboard.briefing", 120, _latest_briefing),
    ),
    300: (
        _source("github", 300),
        _source("openrouter", 300),
        _source("protonmail", 300),
        _source("calendar", 300),
        Collector("dashboard.mail", 600, _mail_status),
    ),
    600: (
        _source("weather", 600),
        Collector("dashboard.weather", 1200, lambda: _fetch("weather")),
    ),
}


async def _broadcast_state_update(key: str, freshness: str, observed_at: str | None) -> None:
    try:
        from backend.api.agents import state_ws_manager

        msg = json.dumps({"type": "state.updated", "key": key, "freshness": freshness, "observed_at": observed_at})
        await state_ws_manager.broadcast(msg)
    except Exception as e:  # best-effort, matches events.publish's contract
        logger.debug(f"state_workers broadcast failed (ignored): {e}")


async def refresh_collector(collector: Collector) -> dict:
    try:
        payload = jsonable_encoder(await collector.load())
        snapshot = await asyncio.to_thread(store_success, collector.key, payload, collector.ttl_seconds)
    except Exception as exc:
        logger.warning("State collector %s failed: %s", collector.key, exc)
        try:
            snapshot = await asyncio.to_thread(store_failure, collector.key, str(exc))
        except Exception as store_exc:
            # A broken state store must not abort the rest of this sequential
            # refresh group. Nothing durable to read in this case, but still
            # emit an unavailable event so connected clients re-read once
            # storage recovers.
            logger.error("State collector %s could not record failure: %s", collector.key, store_exc)
            snapshot = {"key": collector.key, "freshness": "unavailable", "observed_at": None, "error": str(store_exc)}
    await _broadcast_state_update(collector.key, snapshot["freshness"], snapshot.get("observed_at"))
    return snapshot


async def refresh_group(interval_seconds: int) -> None:
    for collector in COLLECTOR_GROUPS[interval_seconds]:
        await refresh_collector(collector)


def register_state_workers(scheduler) -> None:
    from apscheduler.triggers.interval import IntervalTrigger

    for seconds in sorted(COLLECTOR_GROUPS):
        # No near-immediate first-run stagger here: prime_state_workers()
        # (called once from main.py's lifespan) already does the eager,
        # sequential cold-start population. Letting APScheduler's default
        # "first run one interval from now" stand means these jobs don't
        # duplicate that work by refetching the same keys seconds later.
        scheduler.add_job(
            refresh_group,
            IntervalTrigger(seconds=seconds),
            args=[seconds],
            id=f"state_refresh_{seconds}s",
            replace_existing=True,
        )


async def prime_state_workers() -> None:
    """Eagerly populate every group once at boot, before the scheduler's own
    staggered first run, so the very first dashboard request after a restart
    has real data instead of `never_observed`. Called as a fire-and-forget
    background task from main.py's lifespan — must never block server boot."""
    for seconds in sorted(COLLECTOR_GROUPS):
        await refresh_group(seconds)
