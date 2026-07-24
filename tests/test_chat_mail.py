"""Tests for the MAIL / MAIL_SEND chat intents (backend/agents/chat.py)."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import SQLModel, create_engine
from sqlalchemy.pool import StaticPool

import backend.database  # noqa: F401,E402 — registers all tables on metadata


def _make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


_LIST_JSON = json.dumps({
    "emails": [
        {"email_id": "97", "subject": "Hello there", "sender": '"Jane Doe" <jane@example.com>', "date": "2026-07-23T15:43:28Z"},
    ],
    "total": 1,
})

_READ_JSON = json.dumps({
    "emails": [
        {"email_id": "97", "subject": "Hello there", "sender": '"Jane Doe" <jane@example.com>', "date": "2026-07-23T15:43:28Z", "body": "Hi, can you send the report?"},
    ],
})


def _db_patches():
    return (
        patch("backend.agents.chat._db_create_conversation", return_value=1),
        patch("backend.agents.chat._db_add_message"),
        patch("backend.agents.chat._db_load_history", return_value=[{"role": "user", "content": "x"}]),
        patch("backend.agents.chat._db_touch_conversation"),
    )


@pytest.mark.asyncio
async def test_mail_list_happy_path(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, return_value=_LIST_JSON) as mock_list, \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_facts:
        mock_haiku.side_effect = [
            '{"intent":"MAIL"}',
            '{"mode":"list","unread_only":false,"limit":5}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "any new email?")

    assert "Jane Doe" in result["reply"]
    assert "Hello there" in result["reply"]
    mock_list.assert_awaited_once()
    mock_facts.assert_not_called()


@pytest.mark.asyncio
async def test_mail_read_path_calls_read_email_with_id(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, return_value=_LIST_JSON), \
         patch("backend.integrations.protonmail.read_email",
               new_callable=AsyncMock, return_value=_READ_JSON) as mock_read:
        mock_haiku.side_effect = [
            '{"intent":"MAIL"}',
            '{"mode":"read","from_address":"jane@example.com"}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "read the last email from jane")

    mock_read.assert_awaited_once_with("97")
    assert "Hi, can you send the report?" in result["reply"]
    assert "Jane Doe" in result["reply"]


@pytest.mark.asyncio
async def test_mail_integration_failure_friendly_reply(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, side_effect=RuntimeError("mcp down")):
        mock_haiku.side_effect = [
            '{"intent":"MAIL"}',
            '{"mode":"list"}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "any new email?")

    assert "unreachable" in result["reply"].lower()


@pytest.mark.asyncio
async def test_mail_send_happy_path_calls_broker(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()

    class _Res:
        from backend.safety.broker import Decision
        decision = Decision.EXECUTED
        error = None

    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock, return_value=_Res()) as mock_exec:
        mock_haiku.side_effect = [
            '{"intent":"MAIL_SEND"}',
            '{"recipients":["a@example.com"],"subject":"Hi","body":"Hello there"}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "email a@example.com saying hello there")

    mock_exec.assert_awaited_once()
    _, kwargs = mock_exec.call_args
    assert kwargs["actor"] == "user"
    assert kwargs["kind"] == "protonmail_send"
    assert kwargs["payload"]["recipients"] == ["a@example.com"]
    assert kwargs["payload"]["subject"] == "Hi"
    assert "Sent" in result["reply"]


@pytest.mark.asyncio
async def test_mail_send_missing_field_does_not_call_broker(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_exec:
        mock_haiku.side_effect = [
            '{"intent":"MAIL_SEND"}',
            '{"recipients":[],"subject":"","body":""}',
        ]
        from backend.agents.chat import chat
        result = await chat(1, "send an email")

    mock_exec.assert_not_awaited()
    assert "need" in result["reply"].lower()


@pytest.mark.asyncio
async def test_mail_send_garbage_extraction_does_not_call_broker(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_exec:
        mock_haiku.side_effect = [
            '{"intent":"MAIL_SEND"}',
            "not even json",
        ]
        from backend.agents.chat import chat
        result = await chat(1, "send an email")

    mock_exec.assert_not_awaited()
    assert isinstance(result["reply"], str)


@pytest.mark.asyncio
async def test_mail_intents_excluded_from_fact_extraction(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.integrations.protonmail.list_recent",
               new_callable=AsyncMock, return_value=_LIST_JSON), \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_facts:
        mock_haiku.side_effect = ['{"intent":"MAIL"}', '{"mode":"list"}']
        from backend.agents.chat import chat
        await chat(1, "any new email?")

    mock_facts.assert_not_called()


@pytest.mark.asyncio
async def test_mail_budget_exceeded_returns_friendly_reply(monkeypatch):
    from backend.safety.governor import BudgetExceeded

    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku:
        mock_haiku.side_effect = BudgetExceeded("daily", 30, 25, None)
        from backend.agents.chat import chat
        result = await chat(1, "any new email?")

    assert "spending limit" in result["reply"].lower()
