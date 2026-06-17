"""Tests for Tier 2.4 write tools (backend/agents/write_tools.py).

Covers: home_control and hermes_command dispatchers, decision-to-string mapping,
broker gating (EXECUTED / FORBIDDEN / NEEDS_CONFIRM), input validation, never-raise
guarantee, combined provider functions, and config gating.
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
# Test 1: home_control EXECUTED — autonomy ON, call_service mocked → "OK"
# ===========================================================================

@pytest.mark.asyncio
async def test_home_control_executed(eng):
    """EXECUTED path: autonomy on, light domain → LOW risk → ALLOWED → 'OK'."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _home_control

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as cs:
        result = await _home_control({"entity_id": "light.office", "service": "turn_on"})

    assert result.startswith("OK"), f"expected 'OK …', got: {result!r}"
    cs.assert_awaited_once()

    # ActionLog row must exist with actor="agent", kind="ha_service", decision="executed"
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].actor == "agent"
    assert logs[0].kind == "ha_service"
    assert logs[0].decision == "executed"


# ===========================================================================
# Test 2: home_control FORBIDDEN under kill switch — autonomy OFF
# ===========================================================================

@pytest.mark.asyncio
async def test_home_control_forbidden_kill_switch(eng):
    """Kill switch OFF: agent action returns FORBIDDEN; call_service not called."""
    _seed_state(eng, autonomy=False)

    from backend.agents.write_tools import _home_control

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as cs:
        result = await _home_control({"entity_id": "light.office", "service": "turn_on"})

    # Must start with FORBIDDEN and say not performed
    assert result.startswith("FORBIDDEN"), f"expected FORBIDDEN, got: {result!r}"
    assert "NOT performed" in result
    cs.assert_not_awaited()


# ===========================================================================
# Test 3: home_control HIGH device — needs_confirm → "BLOCKED"
# ===========================================================================

@pytest.mark.asyncio
async def test_home_control_high_device_needs_confirm(eng):
    """lock.front is HIGH risk → NEEDS_CONFIRM for agent → 'BLOCKED'."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _home_control

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as cs:
        result = await _home_control({"entity_id": "lock.front", "service": "turn_off"})

    assert result.startswith("BLOCKED"), f"expected BLOCKED (needs_confirm), got: {result!r}"
    cs.assert_not_awaited()


# ===========================================================================
# Test 4: home_control bad input — no broker call
# ===========================================================================

@pytest.mark.asyncio
async def test_home_control_bad_input_no_broker(eng):
    """Missing service and entity_id without a dot → helpful error, broker not called."""
    from backend.agents.write_tools import _home_control

    with patch(
        "backend.safety.broker.execute_action",
        new_callable=AsyncMock,
    ) as mock_broker:
        # Bad service
        result = await _home_control({"entity_id": "light.office", "service": "explode"})
        assert "service" in result.lower() or "error" in result.lower()
        mock_broker.assert_not_awaited()

        # entity_id without a dot
        result2 = await _home_control({"entity_id": "light_office_no_dot", "service": "turn_on"})
        assert "entity_id" in result2.lower() or "error" in result2.lower()
        mock_broker.assert_not_awaited()


# ===========================================================================
# Test 5: hermes_command unknown verb → "unknown verb" + lists allowed verbs
# ===========================================================================

@pytest.mark.asyncio
async def test_hermes_command_unknown_verb(eng):
    """An unknown verb returns a helpful error listing allowed verbs; broker not called."""
    from backend.agents.write_tools import _hermes_command
    from backend.safety.hermes_actions import allowed_verbs

    with patch(
        "backend.safety.broker.execute_action",
        new_callable=AsyncMock,
    ) as mock_broker:
        result = await _hermes_command({"verb": "totally_fake_verb", "args": {}})

    assert "unknown verb" in result.lower(), f"expected 'unknown verb', got: {result!r}"
    # At least one known verb name should appear in the result
    allowed_names = [v["verb"] for v in allowed_verbs()]
    assert any(name in result for name in allowed_names), (
        f"expected at least one allowed verb name in result; got: {result!r}"
    )
    mock_broker.assert_not_awaited()


# ===========================================================================
# Test 6: hermes_command valid LOW-risk verb EXECUTED
# ===========================================================================

@pytest.mark.asyncio
async def test_hermes_command_low_risk_verb_executed(eng):
    """proxmox_status is LOW risk, no required args → ALLOWED for agent → 'OK'."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _hermes_command

    with patch(
        "backend.integrations.hermes.relay",
        new_callable=AsyncMock,
        return_value="proxmox is healthy",
    ) as rl:
        result = await _hermes_command({"verb": "proxmox_status", "args": {}})

    assert result.startswith("OK"), f"expected 'OK …', got: {result!r}"
    rl.assert_awaited_once_with("check proxmox")


# ===========================================================================
# Test 7: Dispatch never raises — force execute_action to raise, assert error string
# ===========================================================================

@pytest.mark.asyncio
async def test_home_control_never_raises_on_broker_exception(eng):
    """Even if execute_action raises unexpectedly, the tool returns an error string."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _home_control

    with patch(
        "backend.safety.broker.execute_action",
        side_effect=RuntimeError("catastrophic broker failure"),
    ):
        result = await _home_control({"entity_id": "light.test", "service": "toggle"})

    assert isinstance(result, str)
    assert "error" in result.lower() or "catastrophic" in result.lower()


@pytest.mark.asyncio
async def test_hermes_command_never_raises_on_broker_exception(eng):
    """hermes_command also never raises — returns error string on unexpected exception."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _hermes_command

    with patch(
        "backend.safety.broker.execute_action",
        side_effect=RuntimeError("kaboom"),
    ):
        result = await _hermes_command({"verb": "proxmox_status", "args": {}})

    assert isinstance(result, str)
    assert "error" in result.lower() or "kaboom" in result.lower()


# ===========================================================================
# Test 8: Combined providers — all_tool_specs, all_dispatchers, write_tool_names
# ===========================================================================

def test_combined_providers_all_tool_specs():
    """all_tool_specs() == read specs + 5 write specs."""
    from backend.agents.tools import tool_specs
    from backend.agents.write_tools import all_tool_specs, write_tool_names

    read_specs = tool_specs()
    all_specs = all_tool_specs()
    assert len(all_specs) == len(read_specs) + 5, (
        f"expected {len(read_specs) + 5} specs, got {len(all_specs)}"
    )


def test_combined_providers_all_dispatchers():
    """all_dispatchers() contains home_control, hermes_command, plus all read names."""
    from backend.agents.tools import dispatcher_map
    from backend.agents.write_tools import all_dispatchers, write_tool_names

    read_names = set(dispatcher_map().keys())
    all_disp = all_dispatchers()
    assert "home_control" in all_disp
    assert "hermes_command" in all_disp
    assert read_names.issubset(set(all_disp.keys()))


def test_write_tool_names():
    """write_tool_names() returns all five write tool names."""
    from backend.agents.write_tools import write_tool_names

    names = write_tool_names()
    assert "home_control" in names
    assert "hermes_command" in names
    assert "channels_record" in names
    assert "unraid_docker_restart" in names
    assert "obsidian_complete_task" in names
    assert len(names) == 5


# ===========================================================================
# Test 9: Config gating — all_planner_block contains home_control; read-only doesn't
# ===========================================================================

def test_config_gating_planner_block():
    """all_planner_block() contains 'home_control'; planner_tool_block() (read-only) does NOT."""
    from backend.agents.tools import planner_tool_block
    from backend.agents.write_tools import all_planner_block

    read_block = planner_tool_block()
    write_block = all_planner_block()

    assert "home_control" in write_block, "all_planner_block must advertise home_control"
    assert "hermes_command" in write_block, "all_planner_block must advertise hermes_command"
    assert "home_control" not in read_block, "read-only planner block must NOT contain home_control"
    assert "hermes_command" not in read_block, "read-only planner block must NOT contain hermes_command"


# ===========================================================================
# Test 10: tools.py still does NOT import the broker
# ===========================================================================

def test_tools_py_does_not_import_broker():
    """Confirm tools.py has no import of backend.safety.broker (read-only guarantee)."""
    import inspect
    from backend.agents import tools

    module_src = inspect.getsource(tools)
    import_lines = [
        ln for ln in module_src.splitlines()
        if ln.strip().startswith(("import ", "from "))
    ]
    for ln in import_lines:
        assert "broker" not in ln, f"tools.py imports the broker: {ln!r}"


# ===========================================================================
# Test 11: actor is "agent" on every execute_action call
# ===========================================================================

@pytest.mark.asyncio
async def test_home_control_actor_is_agent(eng):
    """The actor passed to execute_action is always 'agent' (not 'user' or 'autonomous')."""
    _seed_state(eng, autonomy=True)

    captured = {}

    from backend.safety.broker import execute_action as real_ea

    async def spy_execute_action(**kwargs):
        captured["actor"] = kwargs.get("actor")
        return await real_ea(**kwargs)

    from backend.agents.write_tools import _home_control

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ):
        # We patch the module-level execute_action inside write_tools
        with patch(
            "backend.safety.broker.execute_action",
            side_effect=real_ea,
            wraps=real_ea,
        ) as mock_ea:
            await _home_control({"entity_id": "light.office", "service": "turn_on"})
            call_kwargs = mock_ea.call_args
            # actor may be positional or keyword
            if call_kwargs.args:
                actor_val = call_kwargs.args[0]
            else:
                actor_val = call_kwargs.kwargs.get("actor")
            assert str(actor_val) == "agent", f"expected actor='agent', got {actor_val!r}"


@pytest.mark.asyncio
async def test_hermes_command_actor_is_agent(eng):
    """hermes_command also passes actor='agent' to execute_action."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import _hermes_command
    from backend.safety.broker import execute_action as real_ea

    with patch(
        "backend.integrations.hermes.relay",
        new_callable=AsyncMock,
        return_value="ok",
    ):
        with patch(
            "backend.safety.broker.execute_action",
            side_effect=real_ea,
            wraps=real_ea,
        ) as mock_ea:
            await _hermes_command({"verb": "proxmox_status", "args": {}})
            call_kwargs = mock_ea.call_args
            if call_kwargs.args:
                actor_val = call_kwargs.args[0]
            else:
                actor_val = call_kwargs.kwargs.get("actor")
            assert str(actor_val) == "agent", f"expected actor='agent', got {actor_val!r}"


# ===========================================================================
# Test 12: write_tools.py does NOT expose execute_action inside tools.py namespace
# (belt-and-suspenders: tools.py is still clean)
# ===========================================================================

def test_write_tools_module_imports_broker_not_tools():
    """write_tools imports from tools.py but dispatches through broker — confirmed
    by checking that write_tools exposes the broker's Decision enum path at runtime."""
    from backend.agents import write_tools  # noqa: F401
    from backend.safety.broker import Decision

    # _decision_to_str is exercised; just confirm it handles all Decision values.
    from backend.agents.write_tools import _decision_to_str
    from dataclasses import dataclass

    @dataclass
    class FakeResult:
        decision: Decision
        result: dict | None = None
        error: str | None = None

    assert "OK" in _decision_to_str(FakeResult(decision=Decision.EXECUTED, result={}))
    assert "FAILED" in _decision_to_str(FakeResult(decision=Decision.FAILED, error="oops"))
    assert "BLOCKED" in _decision_to_str(FakeResult(decision=Decision.NEEDS_CONFIRM))
    assert "FORBIDDEN" in _decision_to_str(FakeResult(decision=Decision.FORBIDDEN, error="policy"))


# ===========================================================================
# BLOCKER #2 Tests — idempotency key threading
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 13: Key threading — same args → same key; different args → different key;
#          no context set → key is None.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idem_key_threading_home_control(eng):
    """set_write_context makes _idem_key_for return a stable key tied to
    (task_id, step_index, tool_name, args); distinct args → distinct key;
    no context → None."""
    from backend.agents.write_tools import (
        _idem_key_for,
        set_write_context,
        reset_write_context,
    )

    # No context → None
    assert _idem_key_for("home_control", {"entity_id": "light.x", "service": "turn_on"}) is None

    tok = set_write_context(5, 2)
    try:
        k1 = _idem_key_for("home_control", {"entity_id": "light.x", "service": "turn_on"})
        k2 = _idem_key_for("home_control", {"entity_id": "light.x", "service": "turn_on"})
        k3 = _idem_key_for("home_control", {"entity_id": "light.x", "service": "turn_off"})

        assert k1 is not None and isinstance(k1, str) and len(k1) > 0
        assert k1 == k2, "same args should produce the same key"
        assert k1 != k3, "different service arg should produce a different key"
    finally:
        reset_write_context(tok)

    # After reset → no context → None again
    assert _idem_key_for("home_control", {"entity_id": "light.x", "service": "turn_on"}) is None


@pytest.mark.asyncio
async def test_idem_key_passed_to_broker(eng):
    """With write context set, _home_control passes a non-None idempotency_key to
    execute_action; without context it passes None."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import (
        _home_control,
        set_write_context,
        reset_write_context,
    )

    captured_keys = []

    from backend.safety.broker import execute_action as real_ea

    async def capturing_ea(*args, **kwargs):
        captured_keys.append(kwargs.get("idempotency_key"))
        return await real_ea(*args, **kwargs)

    with patch(
        "backend.safety.broker.execute_action",
        side_effect=capturing_ea,
    ):
        with patch(
            "backend.integrations.homeassistant.call_service",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ):
            # Without context → key should be None
            await _home_control({"entity_id": "light.office", "service": "turn_on"})
            assert captured_keys[-1] is None, f"expected None without context, got {captured_keys[-1]!r}"

            # With context → key should be a non-empty string
            tok = set_write_context(7, 3)
            try:
                await _home_control({"entity_id": "light.office", "service": "turn_on"})
                assert captured_keys[-1] is not None and len(captured_keys[-1]) > 0, \
                    f"expected non-None key with context, got {captured_keys[-1]!r}"
            finally:
                reset_write_context(tok)


# ---------------------------------------------------------------------------
# Test 14: End-to-end replay — real broker idempotency prevents double-fire
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_home_control_idempotency_replay_prevents_double_fire(eng):
    """Call _home_control TWICE with the same write context and args. The broker's
    idempotency replay means call_service is only invoked ONCE (not twice), and
    there is exactly ONE executed ActionLog row."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import (
        _home_control,
        set_write_context,
        reset_write_context,
    )

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ) as cs:
        tok = set_write_context(7, 1)
        try:
            result1 = await _home_control({"entity_id": "light.office", "service": "turn_on"})
            result2 = await _home_control({"entity_id": "light.office", "service": "turn_on"})
        finally:
            reset_write_context(tok)

    # call_service must have been called exactly ONCE (second call replayed by broker)
    cs.assert_awaited_once()

    # Both results should indicate success
    assert result1.startswith("OK"), f"first call should succeed: {result1!r}"
    assert result2.startswith("OK"), f"replayed call should succeed: {result2!r}"

    # Exactly ONE executed ActionLog row (the replay doesn't insert a second row)
    from backend.database import ActionLog
    with Session(eng) as s:
        logs = s.exec(
            select(ActionLog).where(ActionLog.decision == "executed")
        ).all()
    assert len(logs) == 1, f"expected exactly 1 executed ActionLog, got {len(logs)}"


# ---------------------------------------------------------------------------
# Test 15: Different step context produces different keys (cross-step isolation)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idem_keys_differ_across_steps(eng):
    """Same tool + same args but different step_index produce different keys,
    so a tool call in step 1 can't replay a call in step 2."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import (
        _idem_key_for,
        set_write_context,
        reset_write_context,
    )

    tok1 = set_write_context(10, 1)
    key_step1 = _idem_key_for("home_control", {"entity_id": "light.x", "service": "turn_on"})
    reset_write_context(tok1)

    tok2 = set_write_context(10, 2)
    key_step2 = _idem_key_for("home_control", {"entity_id": "light.x", "service": "turn_on"})
    reset_write_context(tok2)

    assert key_step1 != key_step2, (
        f"step 1 and step 2 keys must differ; got {key_step1!r} and {key_step2!r}"
    )


# ---------------------------------------------------------------------------
# Test 16: Hermes command idempotency key threading
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hermes_command_idem_key_threading(eng):
    """_hermes_command passes idempotency_key when write context is set."""
    _seed_state(eng, autonomy=True)

    from backend.agents.write_tools import (
        _hermes_command,
        set_write_context,
        reset_write_context,
    )

    captured_keys = []

    from backend.safety.broker import execute_action as real_ea

    async def capturing_ea(*args, **kwargs):
        captured_keys.append(kwargs.get("idempotency_key"))
        return await real_ea(*args, **kwargs)

    with patch("backend.safety.broker.execute_action", side_effect=capturing_ea):
        with patch(
            "backend.integrations.hermes.relay",
            new_callable=AsyncMock,
            return_value="proxmox is healthy",
        ):
            # Without context → None
            await _hermes_command({"verb": "proxmox_status", "args": {}})
            assert captured_keys[-1] is None

            # With context → non-None key
            tok = set_write_context(3, 1)
            try:
                await _hermes_command({"verb": "proxmox_status", "args": {}})
                assert captured_keys[-1] is not None and len(captured_keys[-1]) > 0
            finally:
                reset_write_context(tok)
