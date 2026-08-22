"""Obligation tracker (2026-08-21) — closes a gap the outcome tracker didn't
cover: a recurring or one-off real-world obligation (a bill's auto-payment, a
vet follow-up) that today either nags forever unchanged in the vault or
silently vanishes once its date passes, with no state machine for "due ->
confirmed handled."

IMPORTANT, load-bearing caveat: this tracks BRIAN'S OWN CONFIRMATION, not
verified bank/lab truth. NEXUS has no way to independently verify a payment
actually cleared or a vet appointment actually happened — resolving an
obligation's flag (Telegram button, /resolve, or the REST route) only means
"a human said this is handled." `last_confirmed_at` must always be read with
that caveat in mind, never as proof of the underlying event.

Design, deliberately minimal (no RRULE parsing — overkill for this):
- `cadence_description` is free text for DISPLAY only ("monthly", "as needed
  per vet") — never parsed.
- `cadence_days` is an OPTIONAL numeric interval. When set, confirming a due
  obligation advances `next_due_at` by that many days. When unset (a one-off,
  or a cadence too irregular to express as a fixed interval), confirming
  still stamps `last_confirmed_at` (which is what actually prevents
  re-flagging, see `due_obligations` below) but leaves `next_due_at`
  untouched — the obligation simply stays inactive-until-someone-edits-it.
- Overdue detection reuses `last_confirmed_at < next_due_at` as the "not yet
  confirmed for this cycle" test — a confirmation always stamps
  `last_confirmed_at = now`, which is always >= a `next_due_at` that's
  already in the past, so a confirmed obligation naturally stops re-flagging
  without needing `next_due_at` itself to move.

Write path reuses backend/agents/outcomes.py's `record_flag_ex` — same
fingerprint/dedup/cooldown discipline as every other flag source
(homelab_watch, watchdog, briefing). This module does NOT open an
OutcomeFlag directly by writing to the table; `run_obligations_check()`
(called by the scheduler) is the only caller of `record_flag_ex` here.
Confirmation is wired the other direction: `outcomes.resolve_flag()` calls
back into this module's `confirm_obligation()` when it resolves a flag whose
`source == "obligation"` — see that function's docstring for why the hookup
lives there instead of a parallel resolve path.

Follows outcomes.py's shape: sync `_db_*` helpers open their own Session
(re-importing engine from backend.database on every call so tests that
monkeypatch backend.database.engine are honoured), public async functions
call them via asyncio.to_thread. No Session/ORM object crosses an await.
"""
import asyncio
import html
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _row_to_dict(o) -> dict:
    return {
        "id": o.id,
        "title": o.title,
        "category": o.category,
        "cadence_description": o.cadence_description,
        "cadence_days": o.cadence_days,
        "next_due_at": o.next_due_at.isoformat() if o.next_due_at else None,
        "last_confirmed_at": o.last_confirmed_at.isoformat() if o.last_confirmed_at else None,
        "note": o.note,
        "active": o.active,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Sync DB helpers — called exclusively via asyncio.to_thread.
# ---------------------------------------------------------------------------

def _db_create(**fields) -> dict:
    from sqlmodel import Session
    from backend.database import Obligation, engine

    with Session(engine) as session:
        row = Obligation(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def _db_list(active_only: bool) -> list[dict]:
    from sqlmodel import Session, select
    from backend.database import Obligation, engine

    with Session(engine) as session:
        stmt = select(Obligation).order_by(Obligation.next_due_at)  # type: ignore[attr-defined]
        if active_only:
            stmt = stmt.where(Obligation.active == True)  # noqa: E712
        rows = session.exec(stmt).all()
        return [_row_to_dict(r) for r in rows]


def _db_get(obligation_id: int) -> dict | None:
    from sqlmodel import Session
    from backend.database import Obligation, engine

    with Session(engine) as session:
        row = session.get(Obligation, obligation_id)
        return _row_to_dict(row) if row else None


def _db_update(obligation_id: int, **fields) -> dict | None:
    from sqlmodel import Session
    from backend.database import Obligation, engine

    with Session(engine) as session:
        row = session.get(Obligation, obligation_id)
        if row is None:
            return None
        for k, v in fields.items():
            setattr(row, k, v)
        row.updated_at = datetime.utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


def _db_delete(obligation_id: int) -> bool:
    from sqlmodel import Session
    from backend.database import Obligation, engine

    with Session(engine) as session:
        row = session.get(Obligation, obligation_id)
        if row is None:
            return False
        session.delete(row)
        session.commit()
        return True


def _db_due() -> list[dict]:
    """Active obligations whose next_due_at has passed without a qualifying
    confirmation (last_confirmed_at is None, or older than next_due_at)."""
    from sqlmodel import Session, select
    from backend.database import Obligation, engine

    now = datetime.utcnow()
    with Session(engine) as session:
        stmt = (
            select(Obligation)
            .where(Obligation.active == True)  # noqa: E712
            .where(Obligation.next_due_at <= now)  # type: ignore[attr-defined]
        )
        rows = session.exec(stmt).all()
        out = [
            r for r in rows
            if r.last_confirmed_at is None or r.last_confirmed_at < r.next_due_at
        ]
        return [_row_to_dict(r) for r in out]


def _db_confirm(obligation_id: int) -> dict | None:
    from sqlmodel import Session
    from backend.database import Obligation, engine

    with Session(engine) as session:
        row = session.get(Obligation, obligation_id)
        if row is None:
            return None
        now = datetime.utcnow()
        row.last_confirmed_at = now
        # Obligation.note is the STANDING description ("autopay from checking").
        # A one-off resolution note belongs on OutcomeFlag.resolution_note,
        # where resolve_flag already stores it -- writing it here silently
        # destroyed the description on every confirm.
        if row.cadence_days:
            row.next_due_at = now + timedelta(days=row.cadence_days)
        row.updated_at = now
        session.add(row)
        session.commit()
        session.refresh(row)
        return _row_to_dict(row)


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def create_obligation(
    *,
    title: str,
    next_due_at: datetime,
    category: str = "other",
    cadence_description: str = "",
    cadence_days: int | None = None,
    note: str | None = None,
) -> dict:
    return await asyncio.to_thread(
        _db_create,
        title=title,
        category=category,
        cadence_description=cadence_description,
        cadence_days=cadence_days,
        next_due_at=next_due_at,
        note=note,
    )


async def list_obligations(*, active_only: bool = False) -> list[dict]:
    return await asyncio.to_thread(_db_list, active_only)


async def get_obligation(obligation_id: int) -> dict | None:
    return await asyncio.to_thread(_db_get, obligation_id)


async def update_obligation(obligation_id: int, **fields) -> dict | None:
    return await asyncio.to_thread(_db_update, obligation_id, **fields)


async def delete_obligation(obligation_id: int) -> bool:
    return await asyncio.to_thread(_db_delete, obligation_id)


async def due_obligations() -> list[dict]:
    return await asyncio.to_thread(_db_due)


async def confirm_obligation(obligation_id: int) -> dict | None:
    """Stamp last_confirmed_at=now and, for a recurring obligation
    (cadence_days set), advance next_due_at by that many days. Called from
    outcomes.resolve_flag() when a flag with source=="obligation" is resolved
    — see that function's docstring. NEVER raises (best-effort, matches
    every other write path in this module's neighborhood — a confirm failure
    must not un-resolve the flag that triggered it)."""
    try:
        return await asyncio.to_thread(_db_confirm, obligation_id)
    except Exception as e:
        logger.warning(f"confirm_obligation failed (id={obligation_id}): {e}")
        return None


async def run_obligations_check() -> dict:
    """Scheduler entry point (daily, gated by obligations_check_enabled in
    backend/config.py): opens an OutcomeFlag for every active obligation
    that's overdue without a qualifying confirmation, via the EXISTING
    outcomes.record_flag_ex — same fingerprint/dedup/cooldown discipline as
    every other flag source, no parallel notification path. Confirming the
    resulting flag (Telegram `flag:resolved:<id>`, `/resolve`, or
    POST /api/safety/flags/{id}/resolve) is what stamps this obligation
    confirmed — see confirm_obligation / outcomes.resolve_flag."""
    from backend.config import get_settings
    from backend.agents import outcomes
    from backend import events

    # Re-check inside the job, not only at registration -- flipping the flag
    # off at runtime otherwise does nothing until a restart (same discipline
    # as homelab_watch.run_homelab_watch / watchdog.run_watchdog).
    if not getattr(get_settings(), "obligations_check_enabled", True):
        return {"checked": 0, "flagged": 0}

    due = await due_obligations()
    flagged = 0
    for o in due:
        severity = "high" if o["category"] == "medical" else "medium"
        summary = f"Obligation overdue: {o['title']} (due {o['next_due_at']})"
        # `title` is user-supplied and notify_phone sends with parse_mode=HTML
        # whenever app_base_url is set. The DB/frontend copy stays unescaped.
        alert = (
            f"Obligation overdue: {html.escape(str(o['title']))} (due {o['next_due_at']})"
        )
        # Fingerprint is PER DUE CYCLE, not per obligation. A stable
        # f"obligation:{id}" meant one "✗ False alarm" tap armed the 30-day
        # false-positive cooldown in record_flag_ex and silently swallowed the
        # NEXT real due cycle. next_due_at advances on every confirm, so dedup
        # still works within a cycle and correctly resets between them.
        d = await outcomes.record_flag_ex(
            "obligation", f"{o['id']}:{o['next_due_at']}", summary,
            detail=o.get("note"), severity=severity,
        )
        if d["surface"]:
            buttons = None
            if d["id"] is not None:
                buttons = [
                    {"text": "✓ Resolved", "callback_data": f"flag:resolved:{d['id']}"},
                    {"text": "✗ False alarm", "callback_data": f"flag:false_positive:{d['id']}"},
                ]
            await events.notify_phone(alert, kind="obligation_due", buttons=buttons)
            flagged += 1
    return {"checked": len(due), "flagged": flagged}
