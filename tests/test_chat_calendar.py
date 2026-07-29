"""Tests for the CALENDAR chat intent (backend/agents/chat.py)."""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import SQLModel, create_engine
from sqlalchemy.pool import StaticPool

import backend.database  # noqa: F401,E402 — registers all tables on metadata
from backend.integrations.calendar import CalendarData


def _make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _db_patches():
    return (
        patch("backend.agents.chat._db_create_conversation", return_value=1),
        patch("backend.agents.chat._db_add_message"),
        patch("backend.agents.chat._db_load_history", return_value=[{"role": "user", "content": "x"}]),
        patch("backend.agents.chat._db_touch_conversation"),
    )


_DENTIST_EVENT = {"date": date(2026, 8, 18), "time": "2:00 PM", "summary": "Dentist", "is_today": False, "sort_minutes": 840}
_CARDIO_EVENT = {"date": date(2026, 8, 5), "time": "9:00 AM", "summary": "Kaplan Cardiology", "is_today": False, "sort_minutes": 540}


@pytest.mark.asyncio
async def test_calendar_match_returns_event(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.calendar.upcoming", new_callable=AsyncMock,
               return_value=CalendarData(events=[_DENTIST_EVENT], feeds_ok=1, feeds_total=1)) as mock_upcoming:
        mock_haiku.side_effect = [
            '{"intent":"CALENDAR"}',
            '{"days_ahead":90,"keyword":"dentist"}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "when is my dr appointment?")

    assert "Dentist" in result["reply"]
    mock_upcoming.assert_awaited_once_with(90)


@pytest.mark.asyncio
async def test_calendar_no_keyword_match_still_lists_upcoming(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.calendar.upcoming", new_callable=AsyncMock,
               return_value=CalendarData(events=[_CARDIO_EVENT], feeds_ok=1, feeds_total=1)):
        mock_haiku.side_effect = [
            '{"intent":"CALENDAR"}',
            '{"days_ahead":90,"keyword":"dentist"}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "when is my dentist appointment?")

    assert "nothing matching" in result["reply"].lower()
    assert "Kaplan Cardiology" in result["reply"]


@pytest.mark.asyncio
async def test_calendar_empty_window(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.calendar.upcoming", new_callable=AsyncMock,
               return_value=CalendarData(events=[], feeds_ok=1, feeds_total=1)):
        mock_haiku.side_effect = [
            '{"intent":"CALENDAR"}',
            '{"days_ahead":7,"keyword":null}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "what's on my calendar this week?")

    assert "No events in the next" in result["reply"]


@pytest.mark.asyncio
async def test_calendar_integration_failure_friendly_reply(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.calendar.upcoming", new_callable=AsyncMock,
               side_effect=RuntimeError("No calendar feed configured")):
        mock_haiku.side_effect = [
            '{"intent":"CALENDAR"}',
            '{"days_ahead":7,"keyword":null}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "what's on my calendar?")

    assert "unreachable" in result["reply"].lower()


@pytest.mark.asyncio
async def test_calendar_budget_exceeded_returns_friendly_reply(monkeypatch):
    from backend.safety.governor import BudgetExceeded

    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        mock_haiku.side_effect = BudgetExceeded("daily", 30, 25, None)
        from backend.agents.chat import chat
        result = await chat(1, "what's on my calendar?")

    assert "spending limit" in result["reply"].lower()


@pytest.mark.asyncio
async def test_calendar_intent_excluded_from_fact_extraction(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.calendar.upcoming", new_callable=AsyncMock,
               return_value=CalendarData(events=[], feeds_ok=1, feeds_total=1)), \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_facts:
        mock_haiku.side_effect = ['{"intent":"CALENDAR"}', '{"days_ahead":7,"keyword":null}']
        from backend.agents.chat import chat
        await chat(1, "what's on my calendar?")

    mock_facts.assert_not_called()
