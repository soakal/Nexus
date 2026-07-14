import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

# Import the models module so every table is registered on SQLModel.metadata
# before create_all runs (otherwise the first test's engine gets no tables).
import backend.database  # noqa: F401,E402

# ---------------------------------------------------------------------------
# council w-observability — AgentTrace opened/closed around chat(), via the
# generic router.open_trace/close_trace helper (mirrors run_briefing's own
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


def _make_ha():
    ha = MagicMock()
    ha.entities = []
    ha.alerts = []
    return ha


def _make_unraid():
    u = MagicMock()
    u.array_status = "started"
    u.storage_used_gb = 0
    u.storage_total_gb = 0
    u.docker_containers = []
    return u


def _make_channels():
    c = MagicMock()
    c.recording_now = []
    c.storage_used_gb = 0
    c.storage_total_gb = 0
    return c


def _make_adguard():
    a = MagicMock()
    a.blocked_today = 0
    a.blocked_pct = 0
    a.filtering_enabled = True
    return a


def _make_weather():
    w = MagicMock()
    w.summary = "Clear"
    return w


def _patched_chat_deps():
    """Context managers for every dependency a successful CHAT-intent turn
    pulls in, mirroring tests/test_chat_memory.py."""
    return [
        patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=_make_ha()),
        patch("backend.integrations.unraid.fetch", new_callable=AsyncMock, return_value=_make_unraid()),
        patch("backend.integrations.channels_dvr.fetch", new_callable=AsyncMock, return_value=_make_channels()),
        patch("backend.integrations.adguard.fetch", new_callable=AsyncMock, return_value=_make_adguard()),
        patch("backend.integrations.weather.fetch", new_callable=AsyncMock, return_value=_make_weather()),
        patch("backend.agents.memory.vault_recall", new_callable=AsyncMock, return_value=""),
        patch("backend.agents.memory.latest_briefing_seed", new_callable=AsyncMock, return_value=""),
    ]


@pytest.mark.asyncio
async def test_trace_opened_and_closed_ok(eng):
    """A successful chat() turn opens exactly one AgentTrace row (kind='chat',
    label=f'conv:{conversation_id}', task_id=None) and closes it status='ok'
    with ended_at set and no error."""
    from backend.database import AgentTrace

    ctxs = _patched_chat_deps()
    with patch("backend.agents.router.haiku", new_callable=AsyncMock, return_value='{"intent":"CHAT"}'), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value="assistant reply"), \
         patch("backend.agents.chat._db_create_conversation", return_value=42), \
         patch("backend.agents.chat._db_add_message"), \
         patch("backend.agents.chat._db_load_history", return_value=[{"role": "user", "content": "hello"}]), \
         patch("backend.agents.chat._db_touch_conversation"), \
         ctxs[0], ctxs[1], ctxs[2], ctxs[3], ctxs[4], ctxs[5], ctxs[6]:

        from backend.agents.chat import chat

        result = await chat(42, "hello")

    assert result == {"conversation_id": 42, "reply": "assistant reply"}

    with Session(eng) as s:
        traces = s.exec(select(AgentTrace).where(AgentTrace.kind == "chat")).all()
        assert len(traces) == 1
        assert traces[0].label == "conv:42"
        assert traces[0].task_id is None
        assert traces[0].status == "ok"
        assert traces[0].ended_at is not None
        assert traces[0].error is None


@pytest.mark.asyncio
async def test_trace_closed_error_on_unexpected_exception(eng):
    """An unexpected exception raised mid-turn (here: the Haiku intent
    classify call itself fails, which chat() only shields BudgetExceeded
    against) still closes the AgentTrace row status='error' with the
    exception message recorded, and the exception propagates unchanged out
    of chat()."""
    from backend.database import AgentTrace

    with patch("backend.agents.router.haiku", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("backend.agents.chat._db_create_conversation", return_value=7), \
         patch("backend.agents.chat._db_add_message"), \
         patch("backend.agents.chat._db_load_history", return_value=[{"role": "user", "content": "hello"}]), \
         patch("backend.agents.chat._db_touch_conversation"):

        from backend.agents.chat import chat

        with pytest.raises(RuntimeError, match="boom"):
            await chat(7, "hello")

    with Session(eng) as s:
        traces = s.exec(select(AgentTrace).where(AgentTrace.kind == "chat")).all()
        assert len(traces) == 1
        assert traces[0].label == "conv:7"
        assert traces[0].status == "error"
        assert traces[0].error is not None and "boom" in traces[0].error
        assert traces[0].ended_at is not None
