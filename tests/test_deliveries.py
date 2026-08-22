"""Tests for backend/agents/deliveries.py — the ExpectedDelivery heartbeat
tracker (backend/database.py::ExpectedDelivery).

Pattern: in-memory StaticPool engine monkeypatched onto backend.database.engine,
matching test_outcome_flags.py / test_goals.py.
"""
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — register all table metadata


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


@pytest.mark.asyncio
async def test_first_heartbeat_auto_registers_with_defaults(eng):
    """A heartbeat for an unknown name creates the row with the documented
    defaults — no manual pre-registration required."""
    from backend.agents import deliveries

    result = await deliveries.record_heartbeat("brand_new_pipeline")

    assert result["name"] == "brand_new_pipeline"
    assert result["expected_interval_minutes"] == deliveries.DEFAULT_INTERVAL_MINUTES
    assert result["grace_minutes"] == deliveries.DEFAULT_GRACE_MINUTES
    assert result["last_heartbeat_at"] is not None

    from backend.database import ExpectedDelivery
    with Session(eng) as s:
        rows = s.exec(select(ExpectedDelivery)).all()
    assert len(rows) == 1
    assert rows[0].name == "brand_new_pipeline"


@pytest.mark.asyncio
async def test_second_heartbeat_updates_existing_row_not_duplicated(eng):
    """A later heartbeat for an already-registered name updates
    last_heartbeat_at in place — no second row, no reset of the tuned
    interval/grace values."""
    from backend.agents import deliveries
    from backend.database import ExpectedDelivery

    with Session(eng) as s:
        row = ExpectedDelivery(
            name="brain_organizer",
            expected_interval_minutes=60,
            grace_minutes=15,
            last_heartbeat_at=datetime.utcnow() - timedelta(hours=5),
        )
        s.add(row)
        s.commit()

    result = await deliveries.record_heartbeat("brain_organizer")

    with Session(eng) as s:
        rows = s.exec(select(ExpectedDelivery)).all()
    assert len(rows) == 1
    assert rows[0].expected_interval_minutes == 60  # untouched by the heartbeat
    assert rows[0].grace_minutes == 15
    # last_heartbeat_at genuinely moved forward, not just re-stamped to the same value.
    assert (datetime.utcnow() - rows[0].last_heartbeat_at) < timedelta(minutes=1)
    assert result["last_heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_list_deliveries_returns_all_registered(eng):
    from backend.agents import deliveries

    await deliveries.record_heartbeat("a")
    await deliveries.record_heartbeat("b")

    rows = await deliveries.list_deliveries()
    names = {r["name"] for r in rows}
    assert names == {"a", "b"}


def test_is_overdue_true_past_interval_plus_grace():
    from backend.agents import deliveries

    now = datetime.utcnow()
    d = {
        "last_heartbeat_at": (now - timedelta(minutes=200)).isoformat(),
        "expected_interval_minutes": 60,
        "grace_minutes": 30,
    }
    assert deliveries.is_overdue(d, now=now) is True


def test_is_overdue_false_within_interval_plus_grace():
    from backend.agents import deliveries

    now = datetime.utcnow()
    d = {
        "last_heartbeat_at": (now - timedelta(minutes=10)).isoformat(),
        "expected_interval_minutes": 60,
        "grace_minutes": 30,
    }
    assert deliveries.is_overdue(d, now=now) is False


def test_is_overdue_true_when_never_heartbeat():
    from backend.agents import deliveries

    now = datetime.utcnow()
    d = {"last_heartbeat_at": None, "expected_interval_minutes": 60, "grace_minutes": 30}
    assert deliveries.is_overdue(d, now=now) is True
