"""Tests for Tier 2.3b — rolling conversation summarization.

Covers:
  1. Conversation model has summary + summarized_through_id fields.
  2. _maybe_summarize folds messages when count exceeds the window.
  3. _maybe_summarize is a no-op below the threshold (haiku never called).
  4. _maybe_summarize is best-effort (RuntimeError swallowed, summary unchanged).
  5. chat() injects [Earlier conversation summary] into the CHAT branch user_prompt.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    """In-memory SQLite engine with all tables created."""
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_conversation(engine, conv_id: int, n_messages: int):
    """Seed a Conversation row and n_messages ChatMessage rows."""
    from backend.database import ChatMessage, Conversation
    with Session(engine) as s:
        conv = Conversation(id=conv_id, title="Test conv")
        s.add(conv)
        for i in range(n_messages):
            role = "user" if i % 2 == 0 else "assistant"
            s.add(ChatMessage(
                conversation_id=conv_id,
                role=role,
                content=f"message {i}",
            ))
        s.commit()


# ---------------------------------------------------------------------------
# 1. Schema — Conversation model has the new fields
# ---------------------------------------------------------------------------

def test_conversation_has_summary_fields(monkeypatch):
    """Conversation rows must accept and return summary + summarized_through_id."""
    from backend.database import Conversation
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    with Session(eng) as s:
        conv = Conversation(title="test", summary="Initial summary", summarized_through_id=7)
        s.add(conv)
        s.commit()
        s.refresh(conv)
        conv_id = conv.id

    with Session(eng) as s:
        loaded = s.get(Conversation, conv_id)
        assert loaded is not None
        assert loaded.summary == "Initial summary"
        assert loaded.summarized_through_id == 7

    # Also verify that a Conversation with no summary has None defaults
    with Session(eng) as s:
        conv2 = Conversation(title="no summary")
        s.add(conv2)
        s.commit()
        s.refresh(conv2)
        conv2_id = conv2.id

    with Session(eng) as s:
        loaded2 = s.get(Conversation, conv2_id)
        assert loaded2.summary is None
        assert loaded2.summarized_through_id is None


# ---------------------------------------------------------------------------
# 2. _maybe_summarize folds — enough messages to trigger
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_summarize_folds_messages(monkeypatch):
    """With (2*limit + 3) messages, _maybe_summarize folds the oldest batch.

    Uses limit=4 so: 11 total messages, fold = 11 - 4 = 7 oldest messages.
    The through_id must be the id of the 7th message (index 6 in the ASC list).
    """
    from backend.database import Conversation
    from backend.agents.chat import _maybe_summarize

    history_limit = 4
    n_messages = 2 * history_limit + 3  # 11

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    _seed_conversation(eng, conv_id=1, n_messages=n_messages)

    # Capture the ids in ASC order so we know the expected through_id boundary
    from backend.agents.chat import _db_messages_after
    all_msgs = _db_messages_after(1, None)
    assert len(all_msgs) == n_messages

    expected_fold_count = n_messages - history_limit  # 7
    expected_through_id = all_msgs[expected_fold_count - 1]["id"]  # id of msg at index 6

    with patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        mock_haiku.return_value = "ROLLED_UP"
        await _maybe_summarize(1, history_limit)

    with Session(eng) as s:
        conv = s.get(Conversation, 1)
        assert conv.summary == "ROLLED_UP"
        assert conv.summarized_through_id == expected_through_id

    # Confirm the boundary: the last folded message is the (expected_fold_count)th
    # message ascending; messages after that id are the un-folded recent window
    assert conv.summarized_through_id == all_msgs[expected_fold_count - 1]["id"]
    remaining = [m for m in all_msgs if m["id"] > conv.summarized_through_id]
    assert len(remaining) == history_limit


# ---------------------------------------------------------------------------
# 3. _maybe_summarize — below threshold, haiku never called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_summarize_below_threshold_no_haiku(monkeypatch):
    """When n_messages <= limit, _maybe_summarize returns without calling haiku."""
    from backend.database import Conversation
    from backend.agents.chat import _maybe_summarize

    history_limit = 4
    n_messages = history_limit  # exactly at the limit — nothing to fold

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    _seed_conversation(eng, conv_id=2, n_messages=n_messages)

    with patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        await _maybe_summarize(2, history_limit)
        mock_haiku.assert_not_awaited()

    with Session(eng) as s:
        conv = s.get(Conversation, 2)
        assert conv.summary is None
        assert conv.summarized_through_id is None


# ---------------------------------------------------------------------------
# 4. _maybe_summarize — best-effort: RuntimeError swallowed, no raise
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maybe_summarize_swallows_haiku_error(monkeypatch):
    """If haiku raises RuntimeError inside _maybe_summarize, it must NOT propagate."""
    from backend.database import Conversation
    from backend.agents.chat import _maybe_summarize

    history_limit = 2
    n_messages = 10  # well above limit — will attempt haiku

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    _seed_conversation(eng, conv_id=3, n_messages=n_messages)

    with patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        mock_haiku.side_effect = RuntimeError("haiku exploded")
        # Must not raise
        await _maybe_summarize(3, history_limit)

    # Summary stays unchanged (None) because haiku failed
    with Session(eng) as s:
        conv = s.get(Conversation, 3)
        assert conv.summary is None
        assert conv.summarized_through_id is None


# ---------------------------------------------------------------------------
# 5. chat() injects [Earlier conversation summary] into the CHAT branch
# ---------------------------------------------------------------------------

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


@pytest.mark.asyncio
async def test_chat_injects_summary_into_user_prompt(monkeypatch):
    """When a conversation has a stored summary, chat() must include
    '[Earlier conversation summary]' and the summary text in the user_prompt
    passed to sonnet.
    """
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    # Pre-set a summary on the conversation
    THE_SUMMARY = "User prefers dark mode and wants to control lights via voice."
    from backend.database import Conversation
    with Session(eng) as s:
        conv = Conversation(id=10, title="Test", summary=THE_SUMMARY, summarized_through_id=5)
        s.add(conv)
        s.commit()

    captured = {}

    async def mock_sonnet(prompt, *, system=None, web_search=False, **kwargs):
        captured["user_prompt"] = prompt
        captured["system"] = system
        return "sonnet reply"

    with patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.agents.router.sonnet", side_effect=mock_sonnet), \
         patch("backend.integrations.homeassistant.fetch",
               new_callable=AsyncMock, return_value=_make_ha()), \
         patch("backend.integrations.unraid.fetch",
               new_callable=AsyncMock, return_value=_make_unraid()), \
         patch("backend.integrations.channels_dvr.fetch",
               new_callable=AsyncMock, return_value=_make_channels()), \
         patch("backend.integrations.adguard.fetch",
               new_callable=AsyncMock, return_value=_make_adguard()), \
         patch("backend.integrations.weather.fetch",
               new_callable=AsyncMock, return_value=_make_weather()), \
         patch("backend.agents.memory.vault_recall",
               new_callable=AsyncMock, return_value=""), \
         patch("backend.agents.memory.latest_briefing_seed",
               new_callable=AsyncMock, return_value=""), \
         patch("backend.agents.chat._db_add_message"), \
         patch("backend.agents.chat._db_load_history",
               return_value=[{"role": "user", "content": "turn on the lights"}]), \
         patch("backend.agents.chat._db_touch_conversation"), \
         patch("backend.agents.chat._maybe_summarize", new_callable=AsyncMock):
        # haiku returns intent=CHAT for classify; _maybe_summarize is patched to a
        # no-op so the second haiku call does not interfere with the classify mock.
        mock_haiku.return_value = '{"intent":"CHAT"}'

        from backend.agents.chat import chat
        result = await chat(10, "turn on the lights")

    assert result["reply"] == "sonnet reply"
    user_prompt = captured.get("user_prompt", "")
    assert "[Earlier conversation summary]" in user_prompt
    assert THE_SUMMARY in user_prompt
