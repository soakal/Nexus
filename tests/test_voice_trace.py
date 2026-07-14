import pytest
from unittest.mock import AsyncMock, patch

from sqlmodel import SQLModel, Session, create_engine, select
from sqlmodel.pool import StaticPool

# Import the models module so every table is registered on SQLModel.metadata
# before create_all runs (otherwise the first test's engine gets no tables).
import backend.database  # noqa: F401,E402

# ---------------------------------------------------------------------------
# council w-observability — AgentTrace opened/closed around process_audio, via
# the generic router.open_trace/close_trace helper (mirrors chat.py's own
# trace wiring, tested in test_chat_trace.py).
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


@pytest.mark.asyncio
async def test_trace_opened_and_closed_ok(eng):
    """A successful process_audio() run opens exactly one AgentTrace row
    (kind='voice', label='voice_command', task_id=None) and closes it
    status='ok' with ended_at set and no error."""
    from backend.database import AgentTrace

    with patch("backend.agents.voice.transcribe", new_callable=AsyncMock, return_value="what's the weather"), \
         patch("backend.agents.voice.route_intent", new_callable=AsyncMock,
               return_value={"intent": "QUERY", "confidence": 0.9, "extracted_action": "weather", "parameters": {}}), \
         patch("backend.agents.router.sonnet", new_callable=AsyncMock, return_value="It's sunny."):

        from backend.agents.voice import process_audio

        result = await process_audio("/tmp/fake.wav")

    assert result["transcript"] == "what's the weather"
    assert result["response"] == "It's sunny."

    with Session(eng) as s:
        traces = s.exec(select(AgentTrace).where(AgentTrace.kind == "voice")).all()
        assert len(traces) == 1
        assert traces[0].label == "voice_command"
        assert traces[0].task_id is None
        assert traces[0].status == "ok"
        assert traces[0].ended_at is not None
        assert traces[0].error is None


@pytest.mark.asyncio
async def test_trace_closed_error_on_unexpected_exception(eng):
    """An unexpected exception raised mid-run (here: transcribe itself fails)
    still closes the AgentTrace row status='error' with the exception message
    recorded, and the exception propagates unchanged out of process_audio."""
    from backend.database import AgentTrace

    with patch("backend.agents.voice.transcribe", new_callable=AsyncMock, side_effect=RuntimeError("boom")):
        from backend.agents.voice import process_audio

        with pytest.raises(RuntimeError, match="boom"):
            await process_audio("/tmp/fake.wav")

    with Session(eng) as s:
        traces = s.exec(select(AgentTrace).where(AgentTrace.kind == "voice")).all()
        assert len(traces) == 1
        assert traces[0].label == "voice_command"
        assert traces[0].status == "error"
        assert traces[0].error is not None and "boom" in traces[0].error
        assert traces[0].ended_at is not None
