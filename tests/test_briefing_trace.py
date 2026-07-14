import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

# Import the models module so every table is registered on SQLModel.metadata
# before create_all runs (otherwise the first test's engine gets no tables).
import backend.database  # noqa: F401,E402

# ---------------------------------------------------------------------------
# council w-observability — AgentTrace opened/closed around run_briefing, via
# the generic router.open_trace/close_trace helper (mirrors the orchestrator's
# own _open_trace/_close_trace trace wiring, tested in
# test_durable_orchestrator.py::test_trace_opened_and_closed_ok /
# test_trace_closed_error_on_unexpected_exception).
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


def _patched_integrations():
    """Context managers for every integration fetch/sonnet/facts/obsidian/hermes
    dependency run_briefing pulls in, mirroring tests/test_briefing.py."""
    from backend.integrations.homeassistant import HAData
    from backend.integrations.unifi import UniFiData
    from backend.integrations.unraid import UnraidData
    from backend.integrations.obsidian import ObsidianData
    from backend.integrations.github import GitHubData
    from backend.integrations.weather import WeatherData
    from backend.integrations.channels_dvr import ChannelsData
    from backend.integrations.adguard import AdGuardData

    return dict(
        ha=HAData(),
        unifi=UniFiData(),
        unraid=UnraidData(),
        obs=ObsidianData(),
        gh=GitHubData(),
        wx=WeatherData(summary="Clear", high_f=75.0, low_f=60.0),
        channels=ChannelsData(),
        ag=AdGuardData(),
    )


@pytest.mark.asyncio
async def test_trace_opened_and_closed_ok(eng):
    """A successful briefing run opens exactly one AgentTrace row (kind=
    'briefing', task_id=None) and closes it status='ok' with ended_at set and
    no error."""
    from backend.database import AgentTrace

    d = _patched_integrations()
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=d["ha"]), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=d["unifi"]), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=d["unraid"]), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=d["obs"]), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=d["gh"]), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=d["wx"]), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=d["channels"]), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=d["ag"]), \
         patch("backend.integrations.hermes.get_calendar", new_callable=AsyncMock, return_value="cal"), \
         patch("backend.integrations.hermes.get_gmail", new_callable=AsyncMock, return_value="mail"), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value="## Priority Actions\nNone"), \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock), \
         patch("backend.integrations.obsidian.create_note", new_callable=AsyncMock, return_value="NEXUS/Briefings/test.md"), \
         patch("backend.integrations.hermes.notify", new_callable=AsyncMock, return_value=True):

        from backend.agents.briefing import run_briefing

        result = await run_briefing()

    assert result == "## Priority Actions\nNone"

    with Session(eng) as s:
        traces = s.exec(select(AgentTrace).where(AgentTrace.kind == "briefing")).all()
        assert len(traces) == 1
        assert traces[0].label == "daily_briefing"
        assert traces[0].task_id is None
        assert traces[0].status == "ok"
        assert traces[0].ended_at is not None
        assert traces[0].error is None


@pytest.mark.asyncio
async def test_trace_closed_error_on_unexpected_exception(eng):
    """An unexpected exception raised mid-run (here: the LLM call itself
    fails) still closes the AgentTrace row status='error' with the exception
    message recorded, and the exception propagates unchanged out of
    run_briefing."""
    from backend.database import AgentTrace

    d = _patched_integrations()
    with patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=d["ha"]), \
         patch("backend.integrations.unifi.fetch", new_callable=AsyncMock, return_value=d["unifi"]), \
         patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=d["unraid"]), \
         patch("backend.integrations.obsidian.fetch", new_callable=AsyncMock, return_value=d["obs"]), \
         patch("backend.integrations.github.fetch", new_callable=AsyncMock, return_value=d["gh"]), \
         patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=d["wx"]), \
         patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=d["channels"]), \
         patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=d["ag"]), \
         patch("backend.integrations.hermes.get_calendar", new_callable=AsyncMock, return_value="cal"), \
         patch("backend.integrations.hermes.get_gmail", new_callable=AsyncMock, return_value="mail"), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, side_effect=RuntimeError("boom")):

        from backend.agents.briefing import run_briefing

        with pytest.raises(RuntimeError, match="boom"):
            await run_briefing()

    with Session(eng) as s:
        traces = s.exec(select(AgentTrace).where(AgentTrace.kind == "briefing")).all()
        assert len(traces) == 1
        assert traces[0].status == "error"
        assert traces[0].error is not None and "boom" in traces[0].error
        assert traces[0].ended_at is not None
