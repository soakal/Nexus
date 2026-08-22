"""Tests for the resumed TrendSnapshot write/read path (2026-08-21):
backend/scheduler.py::_record_trend_snapshot (daily write) and
backend/agents/briefing.py::_trend_baseline_avg/_gather_trend_baselines (the
7-day-average read side briefing.py's Network Security line consumes).

Uses a real tmp-file SQLite engine (same pattern as
tests/test_backup.py::_make_sample_engine) rather than mocking Session, so the
row shape (source/metric/value) is actually verified, not just "commit was
called".
"""
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select


def _make_engine(db_path):
    from backend.database import TrendSnapshot  # noqa: F401 — registers the table
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# _record_trend_snapshot (write side)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_trend_snapshot_writes_both_rows(tmp_path, monkeypatch):
    from backend.database import TrendSnapshot
    from backend.scheduler import _record_trend_snapshot

    engine = _make_engine(tmp_path / "nexus.db")
    monkeypatch.setattr("backend.database.engine", engine)

    ag_data = SimpleNamespace(blocked_pct=12.3)
    un_data = SimpleNamespace(storage_used_gb=456.7)
    with patch("backend.integrations.adguard.fetch", new=AsyncMock(return_value=ag_data)), \
         patch("backend.integrations.unraid.fetch", new=AsyncMock(return_value=un_data)):
        await _record_trend_snapshot()

    with Session(engine) as s:
        rows = s.exec(select(TrendSnapshot)).all()
    assert len(rows) == 2
    by_source = {r.source: r for r in rows}
    assert by_source["adguard"].metric == "blocked_pct"
    assert by_source["adguard"].value == 12.3
    assert by_source["unraid"].metric == "storage_used_gb"
    assert by_source["unraid"].value == 456.7


@pytest.mark.asyncio
async def test_record_trend_snapshot_one_source_down_still_writes_other(tmp_path, monkeypatch):
    from backend.database import TrendSnapshot
    from backend.scheduler import _record_trend_snapshot

    engine = _make_engine(tmp_path / "nexus.db")
    monkeypatch.setattr("backend.database.engine", engine)

    un_data = SimpleNamespace(storage_used_gb=100.0)
    with patch("backend.integrations.adguard.fetch", new=AsyncMock(side_effect=RuntimeError("adguard down"))), \
         patch("backend.integrations.unraid.fetch", new=AsyncMock(return_value=un_data)):
        await _record_trend_snapshot()  # must not raise

    with Session(engine) as s:
        rows = s.exec(select(TrendSnapshot)).all()
    assert len(rows) == 1
    assert rows[0].source == "unraid"


# ---------------------------------------------------------------------------
# _trend_baseline_avg / _gather_trend_baselines (read side)
# ---------------------------------------------------------------------------

def test_trend_baseline_avg_returns_mean_with_7_plus_days(tmp_path, monkeypatch):
    from backend.database import TrendSnapshot
    from backend.agents.briefing import _trend_baseline_avg

    engine = _make_engine(tmp_path / "nexus.db")
    monkeypatch.setattr("backend.database.engine", engine)

    now = datetime.utcnow()
    with Session(engine) as s:
        for i, value in enumerate([10.0, 20.0, 30.0, 10.0, 20.0, 30.0, 20.0]):
            s.add(TrendSnapshot(
                source="adguard", metric="blocked_pct", value=value,
                captured_at=now - timedelta(days=i),
            ))
        s.commit()

    avg = _trend_baseline_avg("adguard", "blocked_pct")
    assert avg == pytest.approx(20.0)


def test_trend_baseline_avg_returns_none_under_7_days(tmp_path, monkeypatch):
    from backend.database import TrendSnapshot
    from backend.agents.briefing import _trend_baseline_avg

    engine = _make_engine(tmp_path / "nexus.db")
    monkeypatch.setattr("backend.database.engine", engine)

    now = datetime.utcnow()
    with Session(engine) as s:
        for i in range(3):  # feature just shipped -- only 3 days of history
            s.add(TrendSnapshot(
                source="unraid", metric="storage_used_gb", value=500.0,
                captured_at=now - timedelta(days=i),
            ))
        s.commit()

    assert _trend_baseline_avg("unraid", "storage_used_gb") is None


def test_trend_baseline_avg_ignores_rows_older_than_7_days(tmp_path, monkeypatch):
    """A stale row outside the trailing 7-day window must not pollute the
    average -- the 9999.0 row from 30 days ago is excluded, so the mean is
    exactly the in-window value."""
    from backend.database import TrendSnapshot
    from backend.agents.briefing import _trend_baseline_avg

    engine = _make_engine(tmp_path / "nexus.db")
    monkeypatch.setattr("backend.database.engine", engine)

    now = datetime.utcnow()
    with Session(engine) as s:
        for i in range(6):  # only 6 rows inside the window
            s.add(TrendSnapshot(
                source="adguard", metric="blocked_pct", value=10.0,
                captured_at=now - timedelta(days=i),
            ))
        s.add(TrendSnapshot(
            source="adguard", metric="blocked_pct", value=9999.0,
            captured_at=now - timedelta(days=30),
        ))
        s.commit()

    # 6 in-window rows >= min_samples (5); the 30-day-old 9999.0 is excluded.
    assert _trend_baseline_avg("adguard", "blocked_pct") == pytest.approx(10.0)


def test_gather_trend_baselines_never_raises_on_db_failure(monkeypatch):
    from backend.agents import briefing

    monkeypatch.setattr(
        briefing, "_trend_baseline_avg",
        lambda source, metric: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    result = briefing._gather_trend_baselines()
    assert result == {"adguard_blocked_pct": None, "unraid_storage_used_gb": None}
