"""Durable signal for the infisical -> legacy vault secret-fallback event.

The legacy-vault fallback used to be signalled only by a `logger.warning` in
backend/secrets/manager.py, but logs/backend.log and logs/backend.err.log are
truncated fresh on every process restart -- so that signal was worthless for
the one question it existed to answer: "has this fired since 2026-07-24?".
This module makes it durable.

Recording (`record`) is pure in-memory -- safe to call from the asyncio event
loop thread, since get_secret() is fully synchronous and loop-thread-reachable
and a direct SQLite write there is forbidden (see manager.py's docstring and
the plan this module was built from: engine's busy_timeout=30000 could block
the loop up to 30s, get_secret() can be called from inside code that already
holds an open Session, and get_secret() is called during startup BEFORE
create_db_and_tables() runs). The actual DB write happens on a 300s scheduler
drain (backend/scheduler.py::_secret_fallback_drain) plus a lifespan-shutdown
flush (backend/main.py) -- so a normal restart doesn't lose the signal. The
accepted residual gap: a hard process kill (e.g. taskkill) still loses at
most one 300s window of buffered-but-undrained events.

Only the secret KEY NAME is ever stored -- never the value.

All DB imports in this module are LAZY, inside function bodies, to avoid an
import cycle: backend.database -> backend.config -> backend.secrets.manager.
"""

import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
MAX_KEYS = 64                    # distinct secret keys buffered; matches authfail.MAX_SOURCES
_PENDING: dict[str, dict] = {}   # secret_key -> {"backend_from","backend_to","first_at","last_at","count"}
_DROPPED = 0                     # events discarded because MAX_KEYS was full


def record(
    secret_key: str,
    *,
    backend_from: str = "infisical",
    backend_to: str = "vault",
    now: datetime | None = None,
) -> None:
    """Coalesce one fallback event into the in-memory buffer. NEVER raises."""
    global _DROPPED
    try:
        now = now if now is not None else datetime.utcnow()
        secret_key = str(secret_key or "unknown")[:128]
        backend_from = str(backend_from or "")[:32]
        backend_to = str(backend_to or "")[:32]
        with _LOCK:
            entry = _PENDING.get(secret_key)
            if entry is None:
                if len(_PENDING) >= MAX_KEYS:
                    # Drop the NEW key -- the already-buffered keys hold the
                    # earliest evidence, which is what "when did it first
                    # happen" needs.
                    _DROPPED += 1
                    return
                _PENDING[secret_key] = {
                    "backend_from": backend_from,
                    "backend_to": backend_to,
                    "first_at": now,
                    "last_at": now,
                    "count": 1,
                }
            else:
                entry["last_at"] = now
                entry["count"] += 1
                entry["backend_from"] = backend_from
                entry["backend_to"] = backend_to
    except Exception:
        pass


def pending() -> dict:
    """Shallow-copied snapshot of the pending buffer (for tests/diagnostics).
    Never raises; returns {} on any error."""
    try:
        with _LOCK:
            return {k: dict(v) for k, v in _PENDING.items()}
    except Exception:
        return {}


def dropped() -> int:
    """Count of events discarded because MAX_KEYS was full. Never raises."""
    try:
        return _DROPPED
    except Exception:
        return 0


def reset() -> None:
    """Test hook -- clear the pending buffer and zero the dropped counter."""
    global _DROPPED
    with _LOCK:
        _PENDING.clear()
        _DROPPED = 0


def _merge_back(batch: dict) -> None:
    """Private. Merge a failed-drain batch back into _PENDING, combining with
    anything recorded during the drain attempt (count summed, first_at = min,
    last_at = max). Respects MAX_KEYS. Never raises."""
    global _DROPPED
    try:
        with _LOCK:
            for key, info in batch.items():
                entry = _PENDING.get(key)
                if entry is None:
                    if len(_PENDING) >= MAX_KEYS:
                        _DROPPED += 1
                        continue
                    _PENDING[key] = dict(info)
                else:
                    entry["count"] += info.get("count", 0)
                    entry["first_at"] = min(entry["first_at"], info["first_at"])
                    entry["last_at"] = max(entry["last_at"], info["last_at"])
                    entry["backend_from"] = info.get("backend_from", entry["backend_from"])
                    entry["backend_to"] = info.get("backend_to", entry["backend_to"])
    except Exception:
        pass


def drain() -> int:
    """Flush the in-memory buffer into SecretFallback rows. Sync, NEVER
    raises. Returns the number of keys committed (0 if the buffer was empty
    or the write failed -- a failed drain requeues the batch so events are
    not lost)."""
    try:
        with _LOCK:
            batch = dict(_PENDING)
            _PENDING.clear()
    except Exception:
        return 0

    if not batch:
        return 0

    try:
        from sqlmodel import Session, select
        from backend.database import SecretFallback, engine

        with Session(engine) as session:
            try:
                for key, info in batch.items():
                    row = session.exec(
                        select(SecretFallback).where(SecretFallback.secret_key == key)
                    ).first()
                    if row is not None:
                        row.event_count += info["count"]
                        row.last_at = max(row.last_at, info["last_at"])
                        row.first_at = min(row.first_at, info["first_at"])
                        row.backend_from = info["backend_from"]
                        row.backend_to = info["backend_to"]
                        session.add(row)
                    else:
                        session.add(SecretFallback(
                            secret_key=key,
                            backend_from=info["backend_from"],
                            backend_to=info["backend_to"],
                            event_count=info["count"],
                            first_at=info["first_at"],
                            last_at=info["last_at"],
                        ))
                session.commit()
            except Exception as e:
                from sqlalchemy.exc import IntegrityError
                if isinstance(e, IntegrityError):
                    # A racing insert on the unique secret_key -- rollback and
                    # apply the update path instead.
                    session.rollback()
                    for key, info in batch.items():
                        row = session.exec(
                            select(SecretFallback).where(SecretFallback.secret_key == key)
                        ).first()
                        if row is not None:
                            row.event_count += info["count"]
                            row.last_at = max(row.last_at, info["last_at"])
                            row.first_at = min(row.first_at, info["first_at"])
                            row.backend_from = info["backend_from"]
                            row.backend_to = info["backend_to"]
                            session.add(row)
                        else:
                            session.add(SecretFallback(
                                secret_key=key,
                                backend_from=info["backend_from"],
                                backend_to=info["backend_to"],
                                event_count=info["count"],
                                first_at=info["first_at"],
                                last_at=info["last_at"],
                            ))
                    session.commit()
                else:
                    raise

        n = len(batch)
        if n > 0:
            logger.info(f"fallback_log: persisted {n} secret-fallback key(s)")
        return n
    except Exception as e:
        logger.warning(f"fallback_log.drain failed (best-effort, requeued): {e}")
        _merge_back(batch)
        return 0


def summary() -> dict:
    """Read the persisted SecretFallback rows. Sync, NEVER raises."""
    pending_count = 0
    dropped_count = 0
    try:
        pending_count = len(pending())
        dropped_count = dropped()
    except Exception:
        pass

    try:
        from sqlmodel import Session, select
        from backend.database import SecretFallback, engine

        with Session(engine) as session:
            rows = session.exec(select(SecretFallback)).all()

        rows_sorted = sorted(rows, key=lambda r: r.last_at, reverse=True)
        keys = [
            {
                "secret_key": r.secret_key,
                "event_count": r.event_count,
                "backend_from": r.backend_from,
                "backend_to": r.backend_to,
                "first_at": r.first_at.isoformat(),
                "last_at": r.last_at.isoformat(),
            }
            for r in rows_sorted
        ]
        total_events = sum(r.event_count for r in rows)
        last_at = rows_sorted[0].last_at.isoformat() if rows_sorted else None

        return {
            "total_events": total_events,
            "key_count": len(rows),
            "last_at": last_at,
            "keys": keys,
            "pending": pending_count,
            "dropped": dropped_count,
        }
    except Exception as e:
        logger.warning(f"fallback_log.summary failed: {e}")
        return {
            "total_events": 0,
            "key_count": 0,
            "last_at": None,
            "keys": [],
            "pending": pending_count,
            "dropped": dropped_count,
            "error": str(e),
        }
