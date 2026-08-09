"""In-memory, process-local registry of "what is NEXUS doing right now" --
the data model behind the Pulse page (/ws/agent-activity, GET /api/activity).

Zero DB writes, zero LLM calls. Durable history already lives in
AgentTrace/TraceSpan/TaskStep/SpendLog -- this is purely a live snapshot,
lost on restart, same accepted tradeoff as homelab_watch.py's in-memory
state ("its entire useful lifetime is the 60s until the next tick").

Thread-safe: mutators are plain dict/deque ops behind a Lock. This must be
safe to call from a run_in_executor worker thread (router._record_trace_span
runs there), so mutators are sync, never await, never touch the DB/Session,
and never raise -- same best-effort contract as events.publish.
"""
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

_MAX_RING = 200
_LABEL_LIMIT = 200
_STALE_AFTER = timedelta(hours=24)


@dataclass
class ActivityEntry:
    actor_id: str
    actor_type: str  # job | worker | loop | trace | task
    label: str = ""
    status: str = "idle"  # running | idle | ok | error
    started_at: str | None = None
    last_run_at: str | None = None
    last_duration_ms: int | None = None
    last_error: str | None = None
    detail: dict = field(default_factory=dict)
    seq: int = 0


_lock = threading.Lock()
_entries: dict[str, ActivityEntry] = {}
_ring: deque = deque(maxlen=_MAX_RING)
# Same cap as _ring -- NOT a plain list. With no client connected (the
# normal state) the broadcaster's `if not activity_ws_manager.active:
# continue` guard means nothing ever drains this, so an unbounded list would
# grow forever (~2MB per 5000 pulses, verified) for the entire process
# lifetime, then dump its whole backlog onto the event loop in one
# json.dumps + send_text the moment someone finally opens Pulse -- exactly
# the loop-stall class this repo's CLAUDE.md calls its #1 hard-won rule.
_pending_events: deque = deque(maxlen=_MAX_RING)
_dirty: set[str] = set()
# Actor ids removed since the last drain -- entries drop out of _entries
# immediately (remove()/sweep_stale()), but a connected client only learns
# about it via this channel in the next delta (see drain_dirty()).
_removed: set[str] = set()
_seq_counter = 0


def reset_registry() -> None:
    """Test hook — clears all state."""
    global _seq_counter
    with _lock:
        _entries.clear()
        _ring.clear()
        _pending_events.clear()
        _dirty.clear()
        _removed.clear()
        _seq_counter = 0


def _now() -> datetime:
    return datetime.utcnow()


def _next_seq() -> int:
    global _seq_counter
    _seq_counter += 1
    return _seq_counter


def begin(actor_id: str, actor_type: str, label: str = "") -> None:
    """Mark an actor as running. Best-effort, never raises."""
    try:
        with _lock:
            e = _entries.get(actor_id)
            if e is None:
                e = ActivityEntry(actor_id=actor_id, actor_type=actor_type)
                _entries[actor_id] = e
            e.status = "running"
            if label:
                e.label = label[:_LABEL_LIMIT]
            e.started_at = _now().isoformat()
            e.seq = _next_seq()
            _dirty.add(actor_id)
            _removed.discard(actor_id)  # re-begun before the prior remove() was drained
    except Exception as ex:
        logger.debug(f"activity.begin failed (ignored): {ex}")


def end(actor_id: str, ok: bool, error: str | None = None) -> None:
    """Mark a running actor's run as finished. Best-effort, never raises.
    No-op if the actor was never begin()'d (e.g. a MISSED scheduler event)."""
    try:
        with _lock:
            e = _entries.get(actor_id)
            if e is None:
                return
            now = _now()
            if e.started_at:
                try:
                    started = datetime.fromisoformat(e.started_at)
                    e.last_duration_ms = int((now - started).total_seconds() * 1000)
                except (TypeError, ValueError):
                    e.last_duration_ms = None
            e.status = "ok" if ok else "error"
            e.last_run_at = now.isoformat()
            e.last_error = error[:_LABEL_LIMIT] if (error and not ok) else None
            e.started_at = None
            e.seq = _next_seq()
            _dirty.add(actor_id)
    except Exception as ex:
        logger.debug(f"activity.end failed (ignored): {ex}")


def pulse(actor_id: str, event: str, summary: str, detail: dict | None = None) -> None:
    """Record a one-off event in the ticker (llm call, tool call, broker
    action, loop heartbeat) — no begin/end lifecycle. Best-effort, never
    raises. If `detail` is given and the actor already has an entry, REPLACES
    its detail dict (e.g. task step progress) without touching
    status/timestamps -- callers must pass the complete dict each time, same
    as update_detail(). A no-op if the actor has no entry yet."""
    try:
        with _lock:
            row = {
                "ts": _now().isoformat(),
                "actor_id": actor_id,
                "event": str(event)[:80],
                "summary": str(summary)[:_LABEL_LIMIT],
            }
            _ring.append(row)
            _pending_events.append(row)
            if detail is not None:
                e = _entries.get(actor_id)
                if e is not None:
                    e.detail = detail
                    e.seq = _next_seq()
                    _dirty.add(actor_id)
    except Exception as ex:
        logger.debug(f"activity.pulse failed (ignored): {ex}")


def update_detail(actor_id: str, detail: dict) -> None:
    """Update an in-flight actor's detail (e.g. task step progress) without
    a ticker event. No-op if the actor has no entry. Best-effort, never
    raises."""
    try:
        with _lock:
            e = _entries.get(actor_id)
            if e is None:
                return
            e.detail = detail
            e.seq = _next_seq()
            _dirty.add(actor_id)
    except Exception as ex:
        logger.debug(f"activity.update_detail failed (ignored): {ex}")


def remove(actor_id: str) -> None:
    """Remove an actor entirely (e.g. a finished task:{id} — dynamic
    per-task entries shouldn't linger after the task is done). Best-effort,
    never raises. Recorded in `_removed` so a connected client's next delta
    can drop it too -- without this, a client never learns an entry is gone
    (drain_dirty only ever emits entries still present), so e.g. a finished
    task would show as permanently "running" in an open Pulse tab."""
    try:
        with _lock:
            if _entries.pop(actor_id, None) is not None:
                _removed.add(actor_id)
            _dirty.discard(actor_id)
    except Exception as ex:
        logger.debug(f"activity.remove failed (ignored): {ex}")


def sweep_stale() -> int:
    """Backstop: drop any entry whose last activity (last_run_at, or
    started_at if still running) is older than _STALE_AFTER. Returns the
    number dropped. Best-effort, never raises. Called periodically by the
    broadcaster, not on every tick -- this is a leak backstop, not a
    liveness mechanism."""
    try:
        cutoff = _now() - _STALE_AFTER
        dropped = 0
        with _lock:
            for actor_id in list(_entries.keys()):
                e = _entries[actor_id]
                ts = e.started_at or e.last_run_at
                if not ts:
                    continue
                try:
                    if datetime.fromisoformat(ts) < cutoff:
                        del _entries[actor_id]
                        _dirty.discard(actor_id)
                        _removed.add(actor_id)
                        dropped += 1
                except (TypeError, ValueError):
                    continue
        return dropped
    except Exception as ex:
        logger.debug(f"activity.sweep_stale failed (ignored): {ex}")
        return 0


def snapshot() -> dict:
    """Full current state — entries + ticker history — for a fresh
    connection or the REST fallback. Best-effort, never raises."""
    try:
        with _lock:
            return {
                "entries": [asdict(e) for e in _entries.values()],
                "events": list(_ring),
            }
    except Exception as ex:
        logger.debug(f"activity.snapshot failed (ignored): {ex}")
        return {"entries": [], "events": []}


def discard_undelivered() -> None:
    """Drop anything queued for the next delta without delivering it.
    Called by the broadcaster whenever nobody is connected -- a later
    connecting client gets a full authoritative snapshot() instead, making
    any accumulated `_pending_events`/`_removed` state redundant by
    construction. Without this, `_removed` in particular leaks one string
    per remove()/sweep_stale() call forever for as long as nobody has Pulse
    open (the normal state) -- `trace:{kind}:{trace_id}` ids are unique per
    instance, so every completed chat/briefing/proposer/voice trace and
    durable task feeds it. `_dirty`/`_entries` are untouched: they still
    need to reflect current state for snapshot()'s benefit, and `_dirty`
    doesn't leak on its own (remove() already discards from it). Best-effort,
    never raises."""
    try:
        with _lock:
            _pending_events.clear()
            _removed.clear()
    except Exception as ex:
        logger.debug(f"activity.discard_undelivered failed (ignored): {ex}")


def drain_dirty() -> dict | None:
    """Pop everything changed since the last drain, for the coalescing
    broadcaster. Returns None if nothing changed (broadcaster should skip
    the send entirely). `removed` lists actor_ids a connected client should
    delete from its own state -- entries drop out of the registry
    immediately on remove()/sweep_stale(), this is what tells a client that
    already happened (see remove()'s docstring). Best-effort, never raises."""
    try:
        with _lock:
            if not _dirty and not _pending_events and not _removed:
                return None
            changed = [asdict(_entries[aid]) for aid in _dirty if aid in _entries]
            events = list(_pending_events)
            removed = list(_removed)
            _dirty.clear()
            _pending_events.clear()
            _removed.clear()
            return {"entries": changed, "events": events, "removed": removed}
    except Exception as ex:
        logger.debug(f"activity.drain_dirty failed (ignored): {ex}")
        return None


async def run_activity_broadcaster() -> None:
    """Coalescing broadcast loop for the Pulse page. Wakes every 250ms;
    broadcasts one delta message only if something changed AND at least one
    client is connected (an unwatched Pulse page costs a timer wake and
    nothing else). A leak-backstop staleness sweep piggybacks every ~60
    ticks (~15s). Started/cancelled from main.py's lifespan. Never raises —
    a broadcast failure is caught and logged, the loop keeps running."""
    import asyncio
    import json

    from backend.api.agents import activity_ws_manager

    tick = 0
    while True:
        await asyncio.sleep(0.25)
        tick += 1
        try:
            if tick % 60 == 0:
                sweep_stale()
            if not activity_ws_manager.active:
                discard_undelivered()
                continue
            delta = drain_dirty()
            if delta is None:
                continue
            msg = json.dumps({"type": "activity.delta", **delta})
            await activity_ws_manager.broadcast(msg)
        except asyncio.CancelledError:
            raise
        except Exception as ex:
            logger.debug(f"activity broadcaster tick failed (ignored): {ex}")
