"""Expected-delivery heartbeat tracker (backend/database.py::ExpectedDelivery).

Closes a blind spot neither watchdog.check_scheduler_stalls nor a health
check covers: a scheduled job can fire on schedule (next_run_time looks
fine) and still silently produce nothing, and a pipeline that lives entirely
outside NEXUS's own APScheduler — e.g. the devbox cron digest relay (cloud
routine -> PR -> devbox cron auto-merge -> vault/Telegram, see this repo's
"nexus-digest-health-check" skill) — has zero visibility here at all today.
A producer pings record_heartbeat(name) (in-process) or POSTs
/api/deliveries/{name}/heartbeat (out-of-process/subprocess/external cron)
on every successful completion; watchdog.check_expected_deliveries() pages
via outcomes.record_flag_ex() when a registered delivery goes overdue.

FOLLOW-UP (not done here, out of this repo's scope): the devbox cron digest
relay itself is not yet wired to call the heartbeat endpoint — it's an
external script on a separate host, not tracked in this repo. Wiring it is
one `curl -X POST .../api/deliveries/claude_digest_relay/heartbeat` call
added to that cron script by hand.

Follows the established shape (outcomes.py/goals.py/digest.py): sync `_db_*`
helpers open their own Session (re-importing engine from backend.database on
every call so tests that monkeypatch backend.database.engine are honoured),
public async functions call them exclusively via asyncio.to_thread. No
Session/ORM object ever crosses an await boundary.
"""
import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Sensible defaults for a delivery auto-registered on its first heartbeat —
# every current/expected producer (brain_organizer, morning_briefing, the
# devbox digest relay) is a once-a-day pipeline, so "daily + 2h slack" is a
# reasonable default for anything not explicitly tuned later.
DEFAULT_INTERVAL_MINUTES = 1440
DEFAULT_GRACE_MINUTES = 120


def _delivery_to_dict(d) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "expected_interval_minutes": d.expected_interval_minutes,
        "grace_minutes": d.grace_minutes,
        "last_heartbeat_at": d.last_heartbeat_at.isoformat() if d.last_heartbeat_at else None,
    }


def _db_heartbeat(name: str) -> dict:
    """Upsert last_heartbeat_at=now. Auto-registers `name` with the defaults
    above on its FIRST heartbeat if it isn't already a known delivery —
    deliberately no manual pre-registration step, so adopting this is one
    line at a producer's completion path."""
    from sqlmodel import Session, select
    from backend.database import ExpectedDelivery, engine

    now = datetime.utcnow()
    with Session(engine) as session:
        row = session.exec(select(ExpectedDelivery).where(ExpectedDelivery.name == name)).first()
        if row is None:
            row = ExpectedDelivery(
                name=name,
                expected_interval_minutes=DEFAULT_INTERVAL_MINUTES,
                grace_minutes=DEFAULT_GRACE_MINUTES,
                last_heartbeat_at=now,
            )
        else:
            row.last_heartbeat_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _delivery_to_dict(row)


def _db_list() -> list[dict]:
    from sqlmodel import Session, select
    from backend.database import ExpectedDelivery, engine

    with Session(engine) as session:
        rows = session.exec(select(ExpectedDelivery)).all()
        return [_delivery_to_dict(r) for r in rows]


async def record_heartbeat(name: str) -> dict:
    """Record a successful completion for `name`, auto-registering it on
    first use. Used both by the in-process producers (briefing.py) and by
    POST /api/deliveries/{name}/heartbeat (backend/api/deliveries.py)."""
    return await asyncio.to_thread(_db_heartbeat, name)


async def list_deliveries() -> list[dict]:
    """Every registered ExpectedDelivery, for the watchdog check to sweep."""
    return await asyncio.to_thread(_db_list)


def is_overdue(d: dict, *, now: datetime | None = None) -> bool:
    """Pure helper (no DB/await) so the watchdog check's staleness logic is
    unit-testable without a Session. True when `now - last_heartbeat_at`
    exceeds expected_interval_minutes + grace_minutes."""
    now = now or datetime.utcnow()
    last = d["last_heartbeat_at"]
    if last is None:
        return True
    if isinstance(last, str):
        last = datetime.fromisoformat(last)
    max_age = timedelta(minutes=d["expected_interval_minutes"] + d["grace_minutes"])
    return (now - last) > max_age
