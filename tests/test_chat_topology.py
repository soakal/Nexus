"""Tests for the TOPOLOGY chat intent (mirrors test_chat_vault.py)."""

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


def _db_patches():
    return (
        patch("backend.agents.chat._db_create_conversation", return_value=1),
        patch("backend.agents.chat._db_add_message"),
        patch("backend.agents.chat._db_load_history", return_value=[{"role": "user", "content": "x"}]),
        patch("backend.agents.chat._db_touch_conversation"),
    )


@pytest.mark.asyncio
async def test_topology_intent_routes_through_run_with_tools(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_rwt, \
         patch("backend.agents.facts.extract_and_store", new_callable=AsyncMock) as mock_facts:
        mock_haiku.side_effect = ['{"intent":"TOPOLOGY"}']
        mock_rwt.return_value = "CT 209 runs Immich (source: pve guest list)."
        from backend.agents.chat import chat
        result = await chat(1, "what runs on CT 209?")

    assert result["reply"] == "CT 209 runs Immich (source: pve guest list)."
    mock_rwt.assert_awaited_once()
    _, kwargs = mock_rwt.call_args
    assert kwargs["label"] == "chat_topology"
    assert kwargs["web_search"] is False
    tool_names = {spec["name"] for spec in kwargs["tool_specs"]}
    assert tool_names == {"graph_topology", "vault_search", "vault_read_note"}
    assert set(kwargs["dispatch"].keys()) == {"graph_topology", "vault_search", "vault_read_note"}
    assert "what runs on CT 209?" in kwargs["prompt"]
    # TOPOLOGY is a conversational intent -- fact extraction still runs, same as CHAT/NOTE/TASK.
    mock_facts.assert_awaited_once()


@pytest.mark.asyncio
async def test_topology_run_with_tools_failure_friendly_reply(monkeypatch):
    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_rwt:
        mock_haiku.side_effect = ['{"intent":"TOPOLOGY"}']
        mock_rwt.side_effect = RuntimeError("anthropic down")
        from backend.agents.chat import chat
        result = await chat(1, "what depends on Unraid?")

    assert "Couldn't look up the homelab topology" in result["reply"]


@pytest.mark.asyncio
async def test_topology_budget_exceeded_degrades_same_as_every_other_intent(monkeypatch):
    """The TOPOLOGY branch re-raises BudgetExceeded rather than swallowing it
    into a friendly reply (matching MAIL's `except BudgetExceeded: raise`) --
    but the outer handler still catches it and degrades to the standard
    budget-reached reply, same contract as every other intent
    (test_governor.py::test_chat_degrades_on_budget)."""
    from backend.agents import chat as chat_mod
    from backend.safety.governor import BudgetExceeded

    monkeypatch.setattr("backend.database.engine", _make_engine())
    p1, p2, p3, p4 = _db_patches()
    with p1, p2, p3, p4, \
         patch("backend.agents.router.haiku", new_callable=AsyncMock) as mock_haiku, \
         patch("backend.agents.router.run_with_tools", new_callable=AsyncMock) as mock_rwt:
        mock_haiku.side_effect = ['{"intent":"TOPOLOGY"}']
        mock_rwt.side_effect = BudgetExceeded("daily", 99.0, 25.0)
        result = await chat_mod.chat(1, "what runs on CT 209?")

    assert result["reply"] == chat_mod._BUDGET_REACHED_REPLY
