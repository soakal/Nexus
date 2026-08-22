"""Tests for the Obligation tracker (backend/agents/obligations.py,
2026-08-21) — a recurring/one-off "did a human confirm this got handled"
reminder (bill payments, vet follow-ups) that surfaces overdue items through
the EXISTING backend/agents/outcomes.py OutcomeFlag machinery.

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching tests/test_outcome_flags.py.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 -- registers all table metadata


def make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def eng(monkeypatch):
    e = make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _create_open_index(engine):
    """ux_outcomeflag_open isn't created by SQLModel.metadata.create_all()
    alone (only create_db_and_tables() adds it) -- matches
    test_outcome_flags.py's own helper."""
    with Session(engine) as s:
        s.exec(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_outcomeflag_open "
            "ON outcomeflag(fingerprint) WHERE status = 'open' AND fingerprint != ''"
        ))
        s.commit()


def _make_obligation(engine, **overrides) -> int:
    from backend.database import Obligation

    fields = dict(
        title="B of A auto-payment",
        category="bill",
        cadence_description="monthly",
        cadence_days=None,
        next_due_at=datetime.utcnow() - timedelta(days=2),
        last_confirmed_at=None,
        note="Confirm it clears; manually trigger if needed",
        active=True,
    )
    fields.update(overrides)
    with Session(engine) as session:
        row = Obligation(**fields)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row.id


# ---------------------------------------------------------------------------
# run_obligations_check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_overdue_unconfirmed_obligation_opens_a_flag(eng):
    """An active obligation whose next_due_at has passed with no confirming
    last_confirmed_at gets an OutcomeFlag opened via record_flag_ex, and a
    phone push fires."""
    from backend.agents import obligations
    from backend.database import OutcomeFlag

    _create_open_index(eng)
    obligation_id = _make_obligation(eng)

    with patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        result = await obligations.run_obligations_check()

    assert result == {"checked": 1, "flagged": 1}
    mock_notify.assert_awaited_once()
    assert mock_notify.call_args.kwargs.get("kind") == "obligation_due"

    with Session(eng) as session:
        rows = session.exec(select(OutcomeFlag)).all()
    assert len(rows) == 1
    assert rows[0].source == "obligation"
    # `check` is per-due-cycle: f"{obligation_id}:{next_due_at_iso}", not a
    # bare id -- see obligations.run_obligations_check.
    assert rows[0].check.startswith(f"{obligation_id}:")
    assert rows[0].fingerprint.startswith(f"obligation:{obligation_id}:")
    assert rows[0].status == "open"
    assert rows[0].severity == "medium"


@pytest.mark.asyncio
async def test_medical_obligation_flags_high_severity(eng):
    from backend.agents import obligations
    from backend.database import OutcomeFlag

    _create_open_index(eng)
    _make_obligation(eng, title="Charlee abdominal ultrasound + LDDS", category="medical")

    with patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        await obligations.run_obligations_check()

    with Session(eng) as session:
        row = session.exec(select(OutcomeFlag)).first()
    assert row.severity == "high"


@pytest.mark.asyncio
async def test_confirmed_obligation_does_not_reflag(eng):
    """An obligation confirmed AT OR AFTER its due date must not be picked up
    by due_obligations()/run_obligations_check() again."""
    from backend.agents import obligations

    _create_open_index(eng)
    due = datetime.utcnow() - timedelta(days=2)
    _make_obligation(eng, next_due_at=due, last_confirmed_at=due + timedelta(hours=1))

    due_list = await obligations.due_obligations()
    assert due_list == []

    with patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as mock_notify:
        result = await obligations.run_obligations_check()

    assert result == {"checked": 0, "flagged": 0}
    mock_notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_confirmation_still_flags(eng):
    """A last_confirmed_at from BEFORE the current next_due_at (an old
    confirmation from a prior cycle) does not suppress the current cycle."""
    from backend.agents import obligations

    _create_open_index(eng)
    due = datetime.utcnow() - timedelta(days=1)
    _make_obligation(eng, next_due_at=due, last_confirmed_at=due - timedelta(days=30))

    due_list = await obligations.due_obligations()
    assert len(due_list) == 1


@pytest.mark.asyncio
async def test_inactive_obligation_never_flags(eng):
    from backend.agents import obligations

    _create_open_index(eng)
    _make_obligation(eng, active=False)

    assert await obligations.due_obligations() == []


# ---------------------------------------------------------------------------
# resolve_flag -> confirm_obligation hookup
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolving_flag_stamps_confirmed_and_advances_recurring_obligation(eng):
    """Resolving an obligation-sourced flag (the SAME chokepoint the Telegram
    flag:resolved:<id> button / /resolve / POST /flags/{id}/resolve all use)
    stamps last_confirmed_at and, for a recurring obligation (cadence_days
    set), advances next_due_at by that many days."""
    from backend.agents import obligations, outcomes
    from backend.database import Obligation

    _create_open_index(eng)
    old_due = datetime.utcnow() - timedelta(days=2)
    obligation_id = _make_obligation(eng, next_due_at=old_due, cadence_days=30)

    with patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        await obligations.run_obligations_check()

    from backend.database import OutcomeFlag
    with Session(eng) as session:
        flag = session.exec(select(OutcomeFlag)).first()
    assert flag is not None

    before = datetime.utcnow()
    result = await outcomes.resolve_flag(flag.id, "resolved", by="telegram")
    assert result == "resolved"

    with Session(eng) as session:
        row = session.get(Obligation, obligation_id)
    assert row.last_confirmed_at is not None
    assert row.last_confirmed_at >= before
    # Advanced roughly 30 days out from the confirmation, not left at the
    # old overdue date.
    assert row.next_due_at > old_due + timedelta(days=25)


@pytest.mark.asyncio
async def test_resolving_flag_stamps_confirmed_but_leaves_one_off_next_due_at(eng):
    """A one-off obligation (cadence_days=None) gets last_confirmed_at
    stamped but next_due_at is left untouched -- there's no interval to
    advance by."""
    from backend.agents import obligations, outcomes
    from backend.database import Obligation, OutcomeFlag

    _create_open_index(eng)
    old_due = datetime.utcnow() - timedelta(days=2)
    obligation_id = _make_obligation(eng, next_due_at=old_due, cadence_days=None)

    with patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        await obligations.run_obligations_check()

    with Session(eng) as session:
        flag = session.exec(select(OutcomeFlag)).first()

    await outcomes.resolve_flag(flag.id, "resolved")

    with Session(eng) as session:
        row = session.get(Obligation, obligation_id)
    assert row.last_confirmed_at is not None
    assert row.next_due_at == old_due


@pytest.mark.asyncio
async def test_false_positive_resolution_does_not_confirm_obligation(eng):
    """Only status=="resolved" counts as a human confirming the obligation
    was handled -- false_positive must not stamp last_confirmed_at."""
    from backend.agents import obligations, outcomes
    from backend.database import Obligation, OutcomeFlag

    _create_open_index(eng)
    obligation_id = _make_obligation(eng)

    with patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        await obligations.run_obligations_check()

    with Session(eng) as session:
        flag = session.exec(select(OutcomeFlag)).first()

    await outcomes.resolve_flag(flag.id, "false_positive")

    with Session(eng) as session:
        row = session.get(Obligation, obligation_id)
    assert row.last_confirmed_at is None


@pytest.mark.asyncio
async def test_resolve_flag_confirm_failure_does_not_break_resolution(eng, monkeypatch):
    """A confirm_obligation exception must not un-resolve the flag itself --
    resolve_flag's own DB update already committed before the obligation
    hookup runs."""
    from backend.agents import obligations, outcomes

    _create_open_index(eng)
    _make_obligation(eng)

    with patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        await obligations.run_obligations_check()

    from backend.database import OutcomeFlag
    with Session(eng) as session:
        flag = session.exec(select(OutcomeFlag)).first()

    async def boom(*a, **k):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(obligations, "confirm_obligation", boom)
    result = await outcomes.resolve_flag(flag.id, "resolved")
    assert result == "resolved"


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crud_roundtrip(eng):
    from backend.agents import obligations

    created = await obligations.create_obligation(
        title="Charlee ACTH-stim recheck",
        next_due_at=datetime.utcnow() + timedelta(days=14),
        category="medical",
        cadence_description="as needed per vet",
    )
    assert created["id"] is not None
    assert created["active"] is True

    fetched = await obligations.get_obligation(created["id"])
    assert fetched["title"] == "Charlee ACTH-stim recheck"

    updated = await obligations.update_obligation(created["id"], note="rescheduled")
    assert updated["note"] == "rescheduled"

    listed = await obligations.list_obligations(active_only=True)
    assert any(o["id"] == created["id"] for o in listed)

    assert await obligations.delete_obligation(created["id"]) is True
    assert await obligations.get_obligation(created["id"]) is None
    assert await obligations.delete_obligation(created["id"]) is False
