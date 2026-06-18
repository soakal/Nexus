"""Tests for the three non-Hermes broker write dispatchers + executor write tools.

Covers:
  1. classify bands (channels_record LOW, unraid_docker HIGH, obsidian_task LOW).
  2. channels_record via _channels_record — EXECUTED path.
  3. unraid_docker via _unraid_docker_restart — NEEDS_CONFIRM path (HIGH risk).
  4. obsidian_task via _obsidian_complete_task — EXECUTED path.
  5. Kill switch: autonomy OFF → all three return FORBIDDEN, integration fns NOT awaited.
  6. Bad input → helpful error string, broker not called, never raises.
  7. Combined providers: all_tool_specs length, all_dispatchers keys, write_tool_names.
"""

import pytest
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Ensure all tables (incl. ActionLog, SystemState) are registered on metadata.
import backend.database  # noqa: F401,E402


# ---------------------------------------------------------------------------
# Engine + fixture helpers — same pattern as test_safety_broker.py
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


def _all_logs(eng):
    from backend.database import ActionLog

    with Session(eng) as s:
        return s.exec(select(ActionLog).order_by(ActionLog.created_at)).all()


def _seed_state(eng, autonomy: bool = True):
    """Seed a SystemState row with the given autonomy_enabled value."""
    from backend.database import SystemState

    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        row.daily_budget_usd = 25.0
        row.per_task_budget_usd = 5.0
        s.commit()


# ===========================================================================
# 1. classify — pure, no DB needed
# ===========================================================================

def test_classify_channels_record():
    from backend.safety.broker import classify, Risk, Reversibility

    assert classify("channels_record", {}) == (Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE)
    assert classify("channels_record", {"program_id": "999"}) == (Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE)


def test_classify_unraid_docker():
    from backend.safety.broker import classify, Risk, Reversibility

    assert classify("unraid_docker", {}) == (Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE)
    assert classify("unraid_docker", {"container_id": "plex"}) == (Risk.HIGH, Reversibility.REVERSIBLE_BY_INVERSE)


def test_classify_obsidian_task():
    from backend.safety.broker import classify, Risk, Reversibility

    assert classify("obsidian_task", {}) == (Risk.LOW, Reversibility.REVERSIBLE)
    assert classify("obsidian_task", {"note_path": "todo.md", "task_text": "Call dentist"}) == (
        Risk.LOW,
        Reversibility.REVERSIBLE,
    )


# ===========================================================================
# 2. channels_record — EXECUTED path (autonomy ON)
# ===========================================================================

@pytest.mark.asyncio
async def test_channels_record_executed(eng):
    """Autonomy ON, trigger_recording mocked → 'OK', ActionLog executed, trigger called once."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _channels_record

    with patch(
        "backend.integrations.channels_dvr.trigger_recording",
        new_callable=AsyncMock,
        return_value={"ok": 1, "job_id": "abc"},
    ) as tr:
        result = await _channels_record({"program_id": "12345"})

    assert result.startswith("OK"), f"expected 'OK …', got: {result!r}"
    tr.assert_awaited_once_with("12345")

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "agent"
    assert logs[0].kind == "channels_record"
    assert logs[0].decision == "executed"


# ===========================================================================
# 3. unraid_docker — NEEDS_CONFIRM path (HIGH risk, autonomy ON, agent)
# ===========================================================================

@pytest.mark.asyncio
async def test_unraid_docker_restart_needs_confirm(eng):
    """HIGH risk → NEEDS_CONFIRM for agent → 'BLOCKED'; restart_docker NOT awaited."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _unraid_docker_restart

    with patch(
        "backend.integrations.unraid.restart_docker",
        new_callable=AsyncMock,
        return_value=True,
    ) as rd:
        result = await _unraid_docker_restart({"container_id": "plex"})

    assert result.startswith("BLOCKED"), f"expected 'BLOCKED', got: {result!r}"
    assert "NOT performed" in result
    rd.assert_not_awaited()

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "agent"
    assert logs[0].kind == "unraid_docker"
    assert logs[0].decision == "needs_confirm"


# ===========================================================================
# 4. obsidian_task — EXECUTED path (autonomy ON)
# ===========================================================================

@pytest.mark.asyncio
async def test_obsidian_complete_task_executed(eng):
    """Autonomy ON, complete_task mocked → 'OK', complete_task awaited with (note_path, task_text)."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _obsidian_complete_task

    with patch(
        "backend.integrations.obsidian.complete_task",
        new_callable=AsyncMock,
        return_value=None,
    ) as ct:
        result = await _obsidian_complete_task(
            {"note_path": "2026-06-17.md", "task_text": "Call dentist"}
        )

    assert result.startswith("OK"), f"expected 'OK …', got: {result!r}"
    ct.assert_awaited_once_with("2026-06-17.md", "Call dentist")

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "agent"
    assert logs[0].kind == "obsidian_task"
    assert logs[0].decision == "executed"


# ===========================================================================
# 5. Kill switch — autonomy OFF → all three return FORBIDDEN, integrations NOT called
# ===========================================================================

@pytest.mark.asyncio
async def test_channels_record_kill_switch(eng):
    """Autonomy OFF → channels_record returns FORBIDDEN; trigger_recording NOT awaited."""
    _seed_state(eng, autonomy=False)

    from backend.agents.write_tools import _channels_record

    with patch(
        "backend.integrations.channels_dvr.trigger_recording",
        new_callable=AsyncMock,
        return_value={"ok": 1},
    ) as tr:
        result = await _channels_record({"program_id": "12345"})

    assert result.startswith("FORBIDDEN"), f"expected FORBIDDEN, got: {result!r}"
    assert "NOT performed" in result
    tr.assert_not_awaited()


@pytest.mark.asyncio
async def test_unraid_docker_restart_kill_switch(eng):
    """Autonomy OFF → unraid_docker_restart returns FORBIDDEN; restart_docker NOT awaited."""
    _seed_state(eng, autonomy=False)

    from backend.agents.write_tools import _unraid_docker_restart

    with patch(
        "backend.integrations.unraid.restart_docker",
        new_callable=AsyncMock,
        return_value=True,
    ) as rd:
        result = await _unraid_docker_restart({"container_id": "plex"})

    assert result.startswith("FORBIDDEN"), f"expected FORBIDDEN, got: {result!r}"
    assert "NOT performed" in result
    rd.assert_not_awaited()


@pytest.mark.asyncio
async def test_obsidian_complete_task_kill_switch(eng):
    """Autonomy OFF → obsidian_complete_task returns FORBIDDEN; complete_task NOT awaited."""
    _seed_state(eng, autonomy=False)

    from backend.agents.write_tools import _obsidian_complete_task

    with patch(
        "backend.integrations.obsidian.complete_task",
        new_callable=AsyncMock,
        return_value=None,
    ) as ct:
        result = await _obsidian_complete_task(
            {"note_path": "2026-06-17.md", "task_text": "Call dentist"}
        )

    assert result.startswith("FORBIDDEN"), f"expected FORBIDDEN, got: {result!r}"
    assert "NOT performed" in result
    ct.assert_not_awaited()


# ===========================================================================
# 6. Bad input — helpful error, broker NOT called, never raises
# ===========================================================================

@pytest.mark.asyncio
async def test_channels_record_bad_input_missing_program_id(eng):
    """Missing or empty program_id → helpful error string, broker not called."""
    from backend.agents.write_tools import _channels_record

    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_broker:
        # Missing key
        result = await _channels_record({})
        assert "program_id" in result.lower() or "error" in result.lower()
        mock_broker.assert_not_awaited()

        # Empty string
        result2 = await _channels_record({"program_id": ""})
        assert "program_id" in result2.lower() or "error" in result2.lower()
        mock_broker.assert_not_awaited()

    # Must return a str, never raise
    assert isinstance(result, str)
    assert isinstance(result2, str)


@pytest.mark.asyncio
async def test_unraid_docker_restart_bad_input_missing_container_id(eng):
    """Missing or empty container_id → helpful error, broker not called."""
    from backend.agents.write_tools import _unraid_docker_restart

    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_broker:
        result = await _unraid_docker_restart({})
        assert "container_id" in result.lower() or "error" in result.lower()
        mock_broker.assert_not_awaited()

        result2 = await _unraid_docker_restart({"container_id": "   "})
        assert "container_id" in result2.lower() or "error" in result2.lower()
        mock_broker.assert_not_awaited()

    assert isinstance(result, str)
    assert isinstance(result2, str)


@pytest.mark.asyncio
async def test_obsidian_complete_task_bad_input(eng):
    """Missing note_path or task_text → helpful error, broker not called."""
    from backend.agents.write_tools import _obsidian_complete_task

    with patch("backend.safety.broker.execute_action", new_callable=AsyncMock) as mock_broker:
        # Missing note_path
        result = await _obsidian_complete_task({"task_text": "Do something"})
        assert "note_path" in result.lower() or "error" in result.lower()
        mock_broker.assert_not_awaited()

        # Missing task_text
        result2 = await _obsidian_complete_task({"note_path": "todo.md"})
        assert "task_text" in result2.lower() or "error" in result2.lower()
        mock_broker.assert_not_awaited()

        # Both empty
        result3 = await _obsidian_complete_task({})
        assert "error" in result3.lower()
        mock_broker.assert_not_awaited()

    assert isinstance(result, str)
    assert isinstance(result2, str)
    assert isinstance(result3, str)


@pytest.mark.asyncio
async def test_channels_record_never_raises_on_broker_exception(eng):
    """Even if execute_action raises, _channels_record returns an error string."""
    from backend.agents.write_tools import _channels_record

    with patch(
        "backend.safety.broker.execute_action",
        side_effect=RuntimeError("catastrophic"),
    ):
        result = await _channels_record({"program_id": "999"})

    assert isinstance(result, str)
    assert "error" in result.lower() or "catastrophic" in result.lower()


@pytest.mark.asyncio
async def test_unraid_docker_restart_never_raises_on_broker_exception(eng):
    """Even if execute_action raises, _unraid_docker_restart returns an error string."""
    from backend.agents.write_tools import _unraid_docker_restart

    with patch(
        "backend.safety.broker.execute_action",
        side_effect=RuntimeError("kaboom"),
    ):
        result = await _unraid_docker_restart({"container_id": "plex"})

    assert isinstance(result, str)
    assert "error" in result.lower() or "kaboom" in result.lower()


@pytest.mark.asyncio
async def test_obsidian_complete_task_never_raises_on_broker_exception(eng):
    """Even if execute_action raises, _obsidian_complete_task returns an error string."""
    from backend.agents.write_tools import _obsidian_complete_task

    with patch(
        "backend.safety.broker.execute_action",
        side_effect=RuntimeError("vault down"),
    ):
        result = await _obsidian_complete_task(
            {"note_path": "todo.md", "task_text": "Do it"}
        )

    assert isinstance(result, str)
    assert "error" in result.lower() or "vault down" in result.lower()


# ===========================================================================
# 7. Combined providers — all_tool_specs, all_dispatchers, write_tool_names
# ===========================================================================

def test_all_tool_specs_length():
    """all_tool_specs() == read specs + 6 write specs."""
    from backend.agents.tools import tool_specs
    from backend.agents.write_tools import all_tool_specs

    read_specs = tool_specs()
    all_specs = all_tool_specs()
    assert len(all_specs) == len(read_specs) + 6, (
        f"expected {len(read_specs) + 6} specs, got {len(all_specs)}"
    )


def test_all_dispatchers_contains_new_kinds():
    """all_dispatchers() contains the three new broker kinds."""
    from backend.agents.write_tools import all_dispatchers

    disp = all_dispatchers()
    assert "channels_record" in disp, "all_dispatchers must contain 'channels_record'"
    assert "unraid_docker_restart" in disp, "all_dispatchers must contain 'unraid_docker_restart'"
    assert "obsidian_complete_task" in disp, "all_dispatchers must contain 'obsidian_complete_task'"
    # Existing ones must still be present
    assert "home_control" in disp
    assert "hermes_command" in disp


def test_write_tool_names_includes_new_tools():
    """write_tool_names() includes all six write tools."""
    from backend.agents.write_tools import write_tool_names

    names = write_tool_names()
    assert "channels_record" in names
    assert "unraid_docker_restart" in names
    assert "obsidian_complete_task" in names
    assert "home_control" in names
    assert "hermes_command" in names
    assert "send_notification" in names
    assert len(names) == 6


# ===========================================================================
# 8. execute_action direct — confirm broker dispatches to the right integration
# ===========================================================================

@pytest.mark.asyncio
async def test_broker_channels_record_dispatches_direct(eng):
    """execute_action(kind='channels_record') calls channels_dvr.trigger_recording (not hermes)."""
    _seed_state(eng, autonomy=True)

    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.channels_dvr.trigger_recording",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as tr:
        res = await execute_action(
            actor="user",
            kind="channels_record",
            target="999",
            payload={"program_id": "999"},
        )

    assert res.decision == Decision.EXECUTED
    tr.assert_awaited_once_with("999")


@pytest.mark.asyncio
async def test_broker_unraid_docker_dispatches_direct(eng):
    """execute_action(kind='unraid_docker') calls unraid.restart_docker (not hermes)."""
    _seed_state(eng, autonomy=True)

    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.unraid.restart_docker",
        new_callable=AsyncMock,
        return_value=True,
    ) as rd:
        # USER actor is always allowed regardless of risk
        res = await execute_action(
            actor="user",
            kind="unraid_docker",
            target="plex",
            payload={"container_id": "plex"},
        )

    assert res.decision == Decision.EXECUTED
    rd.assert_awaited_once_with("plex")


@pytest.mark.asyncio
async def test_broker_obsidian_task_dispatches_direct(eng):
    """execute_action(kind='obsidian_task') calls obsidian.complete_task (not hermes)."""
    _seed_state(eng, autonomy=True)

    from backend.safety.broker import execute_action, Decision

    with patch(
        "backend.integrations.obsidian.complete_task",
        new_callable=AsyncMock,
        return_value=None,
    ) as ct:
        res = await execute_action(
            actor="user",
            kind="obsidian_task",
            target="todo.md",
            payload={"note_path": "todo.md", "task_text": "Call dentist"},
        )

    assert res.decision == Decision.EXECUTED
    ct.assert_awaited_once_with("todo.md", "Call dentist")
