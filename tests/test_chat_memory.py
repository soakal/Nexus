"""Tests for Tier 2.3 — chat memory injection (backend/agents/memory.py)
and the wiring of memory recall into the CHAT branch of backend/agents/chat.py.
"""
import asyncio
from datetime import datetime, timedelta
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


# ---------------------------------------------------------------------------
# 1. assemble() — pure logic
# ---------------------------------------------------------------------------

def test_assemble_both_empty():
    from backend.agents.memory import assemble
    assert assemble("", "") == ""


def test_assemble_only_vault():
    from backend.agents.memory import assemble
    result = assemble("some vault notes", "")
    assert "[VAULT NOTES]" in result
    assert "some vault notes" in result
    assert "[LATEST BRIEFING]" not in result
    assert "RELEVANT MEMORY" in result


def test_assemble_only_briefing():
    from backend.agents.memory import assemble
    result = assemble("", "briefing content here")
    assert "[LATEST BRIEFING]" in result
    assert "briefing content here" in result
    assert "[VAULT NOTES]" not in result
    assert "RELEVANT MEMORY" in result


def test_assemble_both_present():
    from backend.agents.memory import assemble
    result = assemble("vault notes", "briefing seed")
    assert "[VAULT NOTES]" in result
    assert "vault notes" in result
    assert "[LATEST BRIEFING]" in result
    assert "briefing seed" in result
    assert "RELEVANT MEMORY" in result


def test_assemble_none_values_treated_as_empty():
    from backend.agents.memory import assemble
    # None should be treated like "" (falsy)
    assert assemble(None, None) == ""
    result = assemble("notes", None)
    assert "[VAULT NOTES]" in result
    assert "[LATEST BRIEFING]" not in result


# ---------------------------------------------------------------------------
# 2. vault_recall() — best-effort wrapper
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_vault_recall_returns_empty_on_no_notes_found():
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.return_value = "No notes found matching 'foo'."
        from backend.agents.memory import vault_recall
        result = await vault_recall("foo")
        assert result == ""


@pytest.mark.asyncio
async def test_vault_recall_returns_empty_on_obsidian_token_not_configured():
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.return_value = "Obsidian token not configured."
        from backend.agents.memory import vault_recall
        result = await vault_recall("foo")
        assert result == ""


@pytest.mark.asyncio
async def test_vault_recall_returns_empty_on_vault_unavailable():
    # obsidian.vault_search returns "Vault search unavailable: <err>" on exception
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.return_value = "Vault search unavailable: connection refused"
        from backend.agents.memory import vault_recall
        result = await vault_recall("foo")
        assert result == ""


@pytest.mark.asyncio
async def test_vault_recall_returns_empty_on_vault_not_found():
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.return_value = "Obsidian vault not found at C:/some/path."
        from backend.agents.memory import vault_recall
        result = await vault_recall("foo")
        assert result == ""


@pytest.mark.asyncio
async def test_vault_recall_keeps_note_with_obsidian_in_path():
    # A note whose path starts with "Obsidian" must NOT be dropped
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.return_value = "**Obsidian/setup.md**\nsome content about obsidian"
        from backend.agents.memory import vault_recall
        result = await vault_recall("obsidian")
        assert result != ""


@pytest.mark.asyncio
async def test_vault_recall_returns_empty_on_exception():
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.side_effect = RuntimeError("network down")
        from backend.agents.memory import vault_recall
        result = await vault_recall("foo")
        assert result == ""


@pytest.mark.asyncio
async def test_vault_recall_returns_empty_on_falsy_result():
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.return_value = ""
        from backend.agents.memory import vault_recall
        result = await vault_recall("foo")
        assert result == ""


@pytest.mark.asyncio
async def test_vault_recall_truncates_long_result():
    from backend.agents.memory import VAULT_RECALL_CHARS, vault_recall
    long_text = "A" * (VAULT_RECALL_CHARS + 200)
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.return_value = long_text
        result = await vault_recall("foo")
        assert "[truncated]" in result
        assert len(result) <= VAULT_RECALL_CHARS + len(" ...[truncated]")


@pytest.mark.asyncio
async def test_vault_recall_returns_full_result_within_limit():
    from backend.agents.memory import VAULT_RECALL_CHARS, vault_recall
    short_text = "Relevant note content about the topic"
    with patch("backend.integrations.obsidian.vault_search", new_callable=AsyncMock) as mock_vs:
        mock_vs.return_value = short_text
        result = await vault_recall("topic")
        assert result == short_text
        assert "[truncated]" not in result


# ---------------------------------------------------------------------------
# 3. _db_latest_briefing_text() — sync DB helper
# ---------------------------------------------------------------------------

def test_db_latest_briefing_text_returns_newest(monkeypatch):
    from backend.database import Briefing
    from backend.agents.memory import _db_latest_briefing_text

    eng = _make_engine()
    base = datetime(2026, 1, 1)
    with Session(eng) as s:
        s.add(Briefing(content="older briefing", created_at=base))
        s.add(Briefing(content="newer briefing", created_at=base + timedelta(hours=1)))
        s.commit()

    monkeypatch.setattr("backend.database.engine", eng)
    result = _db_latest_briefing_text()
    assert result == "newer briefing"


def test_db_latest_briefing_text_returns_empty_when_none(monkeypatch):
    from backend.agents.memory import _db_latest_briefing_text

    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)
    result = _db_latest_briefing_text()
    assert result == ""


def test_db_latest_briefing_text_truncates_long_content(monkeypatch):
    from backend.database import Briefing
    from backend.agents.memory import BRIEFING_SEED_CHARS, _db_latest_briefing_text

    eng = _make_engine()
    long_content = "B" * (BRIEFING_SEED_CHARS + 300)
    with Session(eng) as s:
        s.add(Briefing(content=long_content))
        s.commit()

    monkeypatch.setattr("backend.database.engine", eng)
    result = _db_latest_briefing_text()
    assert "[truncated]" in result
    assert len(result) <= BRIEFING_SEED_CHARS + len(" ...[truncated]")


def test_db_latest_briefing_text_returns_empty_on_db_error(monkeypatch):
    """If the DB query raises, _db_latest_briefing_text returns "" (best-effort)."""
    from backend.agents.memory import _db_latest_briefing_text

    # Patch sqlmodel.Session context manager to raise on __enter__
    bad_cm = MagicMock()
    bad_cm.__enter__ = MagicMock(side_effect=RuntimeError("db gone"))
    bad_cm.__exit__ = MagicMock(return_value=False)
    bad_session_cls = MagicMock(return_value=bad_cm)

    monkeypatch.setattr("sqlmodel.Session", bad_session_cls)
    result = _db_latest_briefing_text()
    assert result == ""


# ---------------------------------------------------------------------------
# 4. End-to-end CHAT branch injection
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
async def test_chat_injects_memory_into_system_prompt(monkeypatch):
    """When memory fns return content, the system= kwarg passed to sonnet
    must contain RELEVANT MEMORY, the vault text, and the briefing text."""
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    captured_kwargs = {}

    async def mock_sonnet(prompt, *, system=None, web_search=False, **kwargs):
        captured_kwargs["system"] = system
        return "assistant reply"

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
               new_callable=AsyncMock, return_value="NOTE_ABOUT_X") as mock_vr, \
         patch("backend.agents.memory.latest_briefing_seed",
               new_callable=AsyncMock, return_value="BRIEF_SEED_Y") as mock_bs, \
         patch("backend.agents.chat._db_create_conversation", return_value=1), \
         patch("backend.agents.chat._db_add_message"), \
         patch("backend.agents.chat._db_load_history",
               return_value=[{"role": "user", "content": "hello memory"}]), \
         patch("backend.agents.chat._db_touch_conversation"):

        mock_haiku.return_value = '{"intent":"CHAT"}'

        from backend.agents.chat import chat
        result = await chat(1, "hello memory")

    assert result["reply"] == "assistant reply"
    system = captured_kwargs.get("system", "")
    assert "RELEVANT MEMORY" in system
    assert "NOTE_ABOUT_X" in system
    assert "BRIEF_SEED_Y" in system
    # Also confirm both memory fns were called with the user message
    mock_vr.assert_called_once_with("hello memory")
    mock_bs.assert_called_once()


@pytest.mark.asyncio
async def test_chat_no_memory_omits_relevant_memory_block(monkeypatch):
    """When both memory fns return empty string, system= must NOT contain
    RELEVANT MEMORY — the prompt should read normally without the block."""
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    captured_kwargs = {}

    async def mock_sonnet(prompt, *, system=None, web_search=False, **kwargs):
        captured_kwargs["system"] = system
        return "plain reply"

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
               new_callable=AsyncMock, return_value="") as mock_vr, \
         patch("backend.agents.memory.latest_briefing_seed",
               new_callable=AsyncMock, return_value="") as mock_bs, \
         patch("backend.agents.chat._db_create_conversation", return_value=2), \
         patch("backend.agents.chat._db_add_message"), \
         patch("backend.agents.chat._db_load_history",
               return_value=[{"role": "user", "content": "what time is it"}]), \
         patch("backend.agents.chat._db_touch_conversation"):

        mock_haiku.return_value = '{"intent":"CHAT"}'

        from backend.agents.chat import chat
        result = await chat(2, "what time is it")

    assert result["reply"] == "plain reply"
    system = captured_kwargs.get("system", "")
    assert "RELEVANT MEMORY" not in system
    # The snapshot header must still be present
    assert "LIVE HOMELAB SNAPSHOT:" in system


@pytest.mark.asyncio
async def test_chat_memory_exception_coerced_to_empty(monkeypatch):
    """If memory fns raise (return_exceptions=True in gather), the CHAT branch
    coerces them to "" and still calls sonnet without crashing."""
    eng = _make_engine()
    monkeypatch.setattr("backend.database.engine", eng)

    captured_kwargs = {}

    async def mock_sonnet(prompt, *, system=None, web_search=False, **kwargs):
        captured_kwargs["system"] = system
        return "safe reply"

    async def raise_vault(*a, **kw):
        raise RuntimeError("vault boom")

    async def raise_brief(*a, **kw):
        raise RuntimeError("brief boom")

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
         patch("backend.agents.memory.vault_recall", side_effect=raise_vault), \
         patch("backend.agents.memory.latest_briefing_seed", side_effect=raise_brief), \
         patch("backend.agents.chat._db_create_conversation", return_value=3), \
         patch("backend.agents.chat._db_add_message"), \
         patch("backend.agents.chat._db_load_history",
               return_value=[{"role": "user", "content": "safe question"}]), \
         patch("backend.agents.chat._db_touch_conversation"):

        mock_haiku.return_value = '{"intent":"CHAT"}'

        from backend.agents.chat import chat
        result = await chat(3, "safe question")

    assert result["reply"] == "safe reply"
    system = captured_kwargs.get("system", "")
    assert "RELEVANT MEMORY" not in system
    assert "LIVE HOMELAB SNAPSHOT:" in system
