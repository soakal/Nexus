from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

# Import the models module so every table is registered on SQLModel.metadata
# before create_all runs (otherwise the first test's engine gets no tables).
import backend.database  # noqa: F401,E402

# ---------------------------------------------------------------------------
# council w-observability — AgentTrace opened/closed around propose_goals_tick,
# via the generic router.open_trace/close_trace helper (mirrors run_briefing's
# trace wiring, tested in test_briefing_trace.py).
# ---------------------------------------------------------------------------


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


def _seed_state(eng, autonomy=True):
    from backend.database import SystemState
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        s.commit()


# Minimal fake fetch results that _build_snapshot can handle.
def _fake_fetch():
    return SimpleNamespace(
        entities=[],
        alerts=[],
        docker_containers=[],
        array_status="started",
        storage_used_gb=1.0,
        storage_total_gb=10.0,
        recording_now=[],
        blocked_today=0,
        blocked_pct=0.0,
        filtering_enabled=True,
        summary="Clear, 70°F",
    )


def _mock_integrations(monkeypatch):
    """Patch all five integration fetch() calls to return a fake object."""
    fake = _fake_fetch()

    async def _fetch(*a, **k):
        return fake

    for mod_path in (
        "backend.integrations.homeassistant.fetch",
        "backend.integrations.unraid.fetch",
        "backend.integrations.channels_dvr.fetch",
        "backend.integrations.adguard.fetch",
        "backend.integrations.weather.fetch",
    ):
        monkeypatch.setattr(mod_path, _fetch)


@pytest.mark.asyncio
async def test_trace_opened_and_closed_ok(eng, monkeypatch):
    """A successful tick (Haiku returns an empty proposal array) opens exactly
    one AgentTrace row (kind='proposer', task_id=None) and closes it
    status='ok' with ended_at set and no error."""
    from backend.database import AgentTrace

    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    with patch("backend.agents.router.haiku", new=AsyncMock(return_value="[]")):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "ok"

    with Session(eng) as s:
        traces = s.exec(select(AgentTrace).where(AgentTrace.kind == "proposer")).all()
        assert len(traces) == 1
        assert traces[0].label == "goal_proposer_tick"
        assert traces[0].task_id is None
        assert traces[0].status == "ok"
        assert traces[0].ended_at is not None
        assert traces[0].error is None


@pytest.mark.asyncio
async def test_trace_closed_error_on_unexpected_exception(eng, monkeypatch):
    """An unexpected exception raised mid-tick (here: the Haiku call itself
    fails with something other than BudgetExceeded) still closes the
    AgentTrace row status='error' with the exception message recorded, and
    propose_goals_tick still honors its own best-effort contract (returns
    status='error' rather than raising)."""
    from backend.database import AgentTrace

    _seed_state(eng, autonomy=True)
    _mock_integrations(monkeypatch)

    with patch("backend.agents.router.haiku", new=AsyncMock(side_effect=RuntimeError("boom"))):
        with patch("backend.config.get_settings") as mock_settings:
            s = MagicMock()
            s.proposer_max_per_tick = 3
            s.goal_ttl_seconds = 86400
            s.goal_debounce_seconds = 3600
            s.auto_approve_low_risk = False
            mock_settings.return_value = s

            from backend.agents.proposer import propose_goals_tick
            result = await propose_goals_tick()

    assert result["status"] == "error"
    assert "boom" in result["error"]

    with Session(eng) as s:
        traces = s.exec(select(AgentTrace).where(AgentTrace.kind == "proposer")).all()
        assert len(traces) == 1
        assert traces[0].status == "error"
        assert traces[0].error is not None and "boom" in traces[0].error
        assert traces[0].ended_at is not None
