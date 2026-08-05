"""Durable SQLite state snapshots with a process-local hot cache."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

_memory: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


def reset_memory_cache() -> None:
    with _lock:
        _memory.clear()


def _decode(row) -> dict[str, Any]:
    payload = None
    if row.payload_json is not None:
        try:
            payload = json.loads(row.payload_json)
        except (TypeError, json.JSONDecodeError):
            payload = None
    return {
        "key": row.key,
        "data": payload,
        "stored_status": row.status,
        "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        "attempted_at": row.attempted_at.isoformat() if row.attempted_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "error": row.error,
        "schema_version": row.schema_version,
    }


def _never_observed(key: str) -> dict[str, Any]:
    return {
        "key": key,
        "data": None,
        "stored_status": "never_observed",
        "observed_at": None,
        "attempted_at": None,
        "expires_at": None,
        "error": None,
        "schema_version": 1,
    }


def _with_freshness(value: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.utcnow()
    result = dict(value)
    expires_at = result.get("expires_at")
    expired = False
    if expires_at:
        try:
            expired = datetime.fromisoformat(expires_at) <= now
        except ValueError:
            expired = True

    if result.get("data") is None:
        result["freshness"] = (
            "never_observed" if result.get("stored_status") == "never_observed" else "unavailable"
        )
    elif result.get("stored_status") == "error" or expired:
        result["freshness"] = "stale"
    else:
        result["freshness"] = "fresh"
    return result


def _get_or_create(session, key: str):
    """Fetch a row, or insert a new one -- racing this against another
    coroutine doing the same first-insert for the same key (e.g. the eager
    boot-time prime and the scheduler's own first tick landing close
    together) must not raise: the loser falls back to updating the row the
    winner just created, instead of propagating a raw IntegrityError up into
    a stored `error` field."""
    from backend.database import StateSnapshot

    row = session.get(StateSnapshot, key)
    if row is not None:
        return row
    row = StateSnapshot(key=key)
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        row = session.get(StateSnapshot, key)
    return row


def store_success(key: str, payload: Any, ttl_seconds: int, *, schema_version: int = 1) -> dict[str, Any]:
    """Atomically replace a key after a successful observation."""
    from backend.database import engine

    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    now = datetime.utcnow()
    expires = now + timedelta(seconds=max(1, int(ttl_seconds)))
    with Session(engine) as session:
        row = _get_or_create(session, key)
        row.payload_json = encoded
        row.status = "fresh"
        row.observed_at = now
        row.attempted_at = now
        row.expires_at = expires
        row.error = None
        row.schema_version = schema_version
        session.commit()
        session.refresh(row)
        value = _decode(row)
    with _lock:
        _memory[key] = value
    return _with_freshness(value, now)


def store_failure(key: str, error: str) -> dict[str, Any]:
    """Record a failed attempt while preserving the last successful payload."""
    from backend.database import engine

    now = datetime.utcnow()
    with Session(engine) as session:
        row = _get_or_create(session, key)
        row.status = "error"
        row.attempted_at = now
        row.error = str(error)[:2000]
        session.commit()
        session.refresh(row)
        value = _decode(row)
    with _lock:
        _memory[key] = value
    return _with_freshness(value, now)


def get_snapshot(key: str) -> dict[str, Any]:
    from backend.database import StateSnapshot, engine

    with _lock:
        value = _memory.get(key)
    if value is None:
        with Session(engine) as session:
            row = session.get(StateSnapshot, key)
            if row is None:
                return _with_freshness(_never_observed(key))
            value = _decode(row)
        with _lock:
            _memory[key] = value
    return _with_freshness(value)


def get_snapshots(keys: list[str]) -> dict[str, dict[str, Any]]:
    """Read many keys with at most one SQLite query for cache misses."""
    from backend.database import StateSnapshot, engine

    unique_keys = list(dict.fromkeys(keys))
    with _lock:
        values = {key: _memory[key] for key in unique_keys if key in _memory}
    missing = [key for key in unique_keys if key not in values]

    if missing:
        with Session(engine) as session:
            rows = session.exec(select(StateSnapshot).where(StateSnapshot.key.in_(missing))).all()
        loaded = {row.key: _decode(row) for row in rows}
        for key in missing:
            values[key] = loaded.get(key, _never_observed(key))
        with _lock:
            _memory.update(values)

    return {key: _with_freshness(values[key]) for key in unique_keys}
