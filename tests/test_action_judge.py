"""Action-judge gate tests (backend/safety/judge.py wired into
backend/safety/broker.py::execute_action).

Drives real execute_action calls against an in-memory StaticPool DB (same
pattern as tests/test_safety_broker.py's make_engine/eng fixture). The
conftest autouse `action_judge_off_by_default` fixture forces
action_judge_mode="off" for every test by default; each test here overrides
it per-test via monkeypatch on the real settings singleton (innermost patch
wins, same pattern documented on that fixture).

The judge's LLM call is never real: `backend.agents.router.run_model` is
patched with an AsyncMock (or a plain async stand-in for the timeout case)
returning the judge's expected JSON string.
"""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Ensure all tables (incl. ActionLog) are registered on SQLModel.metadata.
import backend.database  # noqa: F401,E402
from backend.config import get_settings
from backend.safety import throttle
from backend.safety.broker import Actor, Decision, execute_action
from backend.safety.governor import BudgetExceeded


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


@pytest.fixture(autouse=True)
def reset_throttle_state():
    """Clear all throttle/breaker state before and after every test so state
    from a prior test (or another test module sharing the process-local
    throttle dict) can't interfere with the ALLOWED-path judge tests here."""
    throttle.reset()
    yield
    throttle.reset()


def _all_logs(eng):
    from backend.database import ActionLog

    with Session(eng) as s:
        return s.exec(select(ActionLog).order_by(ActionLog.created_at)).all()


def _get_log(eng, log_id):
    from backend.database import ActionLog

    with Session(eng) as s:
        return s.get(ActionLog, log_id)


def _set_judge_mode(monkeypatch, mode: str):
    monkeypatch.setattr(get_settings(), "action_judge_mode", mode)


_APPROVE_JSON = '{"allow": true, "confidence": 0.95, "reason": "looks fine"}'
_VETO_JSON = '{"allow": false, "confidence": 0.9, "reason": "test veto"}'


# ---------------------------------------------------------------------------
# Judge invocation gating — only for agent/autonomous ALLOWED, non-exempt,
# non-confirmed, non-throttled, non-replayed dispatches.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_invoked_for_agent_allowed(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "shadow")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_APPROVE_JSON) as rm, \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None):
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.EXECUTED
    rm.assert_awaited_once()


@pytest.mark.asyncio
async def test_judge_invoked_for_autonomous_allowed(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "shadow")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_APPROVE_JSON) as rm, \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None):
        res = await execute_action(
            actor=Actor.AUTONOMOUS, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.EXECUTED
    rm.assert_awaited_once()


@pytest.mark.asyncio
async def test_judge_not_invoked_for_user_actor(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_VETO_JSON) as rm, \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct:
        res = await execute_action(
            actor=Actor.USER, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.EXECUTED
    rm.assert_not_called()
    ct.assert_awaited_once()


@pytest.mark.asyncio
async def test_judge_not_invoked_when_confirmed(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_VETO_JSON) as rm, \
         patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock, return_value={"ok": True}) as cs:
        res = await execute_action(
            actor=Actor.AGENT, kind="ha_service", target="lock.front_door",
            payload={"domain": "lock", "service": "unlock"},
            confirmed=True,
        )
    assert res.decision == Decision.EXECUTED
    rm.assert_not_called()
    cs.assert_awaited_once()


@pytest.mark.asyncio
async def test_judge_not_invoked_when_throttled(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    with patch("backend.safety.throttle.allow", return_value=(False, "throttled")), \
         patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_VETO_JSON) as rm, \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None):
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.FORBIDDEN
    assert res.error == "throttled"
    rm.assert_not_called()


@pytest.mark.asyncio
async def test_judge_not_invoked_for_idempotency_replay(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    from backend.database import ActionLog

    with Session(eng) as s:
        row = ActionLog(
            actor="agent", kind="obsidian_task", target="vault",
            payload_json=json.dumps({"note_path": "tasks.md", "task_text": "do thing"}),
            risk="low", reversibility="reversible", decision="executed",
            result_json=json.dumps({"ok": True}), idempotency_key="idem-replay-1",
        )
        s.add(row)
        s.commit()

    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_VETO_JSON) as rm, \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct:
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
            idempotency_key="idem-replay-1",
        )
    assert res.decision == Decision.EXECUTED
    assert res.replayed is True
    rm.assert_not_called()
    ct.assert_not_called()


@pytest.mark.asyncio
async def test_judge_not_invoked_for_exempt_kind(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    assert "send_notification" in get_settings().action_judge_exempt_kinds
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_VETO_JSON) as rm, \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as np:
        res = await execute_action(
            actor=Actor.AGENT, kind="send_notification", target="owner",
            payload={"content": "hello"},
        )
    assert res.decision == Decision.EXECUTED
    rm.assert_not_called()
    np.assert_awaited_once()


# ---------------------------------------------------------------------------
# Veto behaviour — enforce vs shadow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_veto_enforce_needs_confirm_notifies_phone(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_VETO_JSON), \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct, \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True) as np:
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.NEEDS_CONFIRM
    ct.assert_not_called()
    np.assert_awaited_once()
    phone_content = np.await_args.args[0]
    assert "test veto" in phone_content

    row = _get_log(eng, res.log_id)
    assert row.decision == "needs_confirm"
    assert row.judge_verdict == "veto"
    assert row.judge_reason == "test veto"


@pytest.mark.asyncio
async def test_judge_veto_shadow_dispatches_normally(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "shadow")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_VETO_JSON), \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct:
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.EXECUTED
    ct.assert_awaited_once()

    row = _get_log(eng, res.log_id)
    assert row.decision == "executed"
    assert row.judge_verdict == "veto"
    assert row.judge_reason == "test veto"


# ---------------------------------------------------------------------------
# Judge fail-safe paths (exception / timeout / BudgetExceeded) — never escape
# execute_action; enforce -> needs_confirm, shadow -> dispatch proceeds.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_generic_exception_enforce_needs_confirm(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct:
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.NEEDS_CONFIRM
    ct.assert_not_called()
    row = _get_log(eng, res.log_id)
    assert row.judge_verdict == "error"


@pytest.mark.asyncio
async def test_judge_timeout_enforce_needs_confirm(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    monkeypatch.setattr(get_settings(), "action_judge_timeout_s", 0.01)

    async def _slow_run_model(*args, **kwargs):
        await asyncio.sleep(0.2)
        return _APPROVE_JSON

    with patch("backend.agents.router.run_model", new=_slow_run_model), \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct:
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.NEEDS_CONFIRM
    ct.assert_not_called()
    row = _get_log(eng, res.log_id)
    assert row.judge_verdict == "error"
    assert "timed out" in row.judge_reason


@pytest.mark.asyncio
async def test_judge_budget_exceeded_enforce_needs_confirm(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "enforce")
    with patch(
        "backend.agents.router.run_model", new_callable=AsyncMock,
        side_effect=BudgetExceeded("daily", 30.0, 25.0),
    ), patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct:
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.NEEDS_CONFIRM
    ct.assert_not_called()
    row = _get_log(eng, res.log_id)
    assert row.judge_verdict == "error"
    assert "budget exceeded" in row.judge_reason


@pytest.mark.asyncio
async def test_judge_exception_shadow_dispatches_normally(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "shadow")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, side_effect=RuntimeError("boom")), \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct:
        res = await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    assert res.decision == Decision.EXECUTED
    ct.assert_awaited_once()
    row = _get_log(eng, res.log_id)
    assert row.judge_verdict == "error"


# ---------------------------------------------------------------------------
# Spend labelling — the judge's model call must be metered under
# label="action_judge" (asserted on the call into router.run_model, which is
# the metered entry point that would attribute a real SpendLog row).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_judge_call_uses_action_judge_spend_label(eng, monkeypatch):
    _set_judge_mode(monkeypatch, "shadow")
    with patch("backend.agents.router.run_model", new_callable=AsyncMock, return_value=_APPROVE_JSON) as rm, \
         patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None):
        await execute_action(
            actor=Actor.AGENT, kind="obsidian_task", target="vault",
            payload={"note_path": "tasks.md", "task_text": "do thing"},
        )
    rm.assert_awaited_once()
    assert rm.await_args.kwargs.get("label") == "action_judge"


# ---------------------------------------------------------------------------
# A judge-vetoed row is confirmable via the existing
# POST /api/safety/actions/{id}/confirm endpoint and then dispatches.
# ---------------------------------------------------------------------------

@pytest.fixture
def safety_client(tmp_path, monkeypatch):
    vault_key = tmp_path / ".vault.key"
    vault_file = tmp_path / "nexus.vault"
    vault_key.write_bytes(b"A" * 32)
    vault_file.write_text("{}")
    monkeypatch.chdir(tmp_path)

    test_engine = make_engine()
    monkeypatch.setattr("backend.database.engine", test_engine)

    def override_session():
        with Session(test_engine) as session:
            yield session

    with patch("backend.database.create_db_and_tables"), \
         patch("backend.scheduler.setup_scheduler"), \
         patch("backend.scheduler.scheduler") as sched, \
         patch("backend.agents.memo_watcher.start_watcher_blocking"), \
         patch("backend.agents.memo_watcher.stop_watcher", new_callable=AsyncMock):
        sched.running = False
        from backend.database import get_session
        from backend.main import app
        app.dependency_overrides[get_session] = override_session
        with TestClient(app) as c:
            c._engine = test_engine
            yield c
        app.dependency_overrides.clear()


def _seed_judge_vetoed_action(eng, **kw):
    from backend.database import ActionLog

    defaults = dict(
        actor="agent", kind="obsidian_task", target="vault",
        payload_json=json.dumps({"note_path": "tasks.md", "task_text": "do thing"}),
        risk="low", reversibility="reversible", decision="needs_confirm",
        result_json=None, idempotency_key=None,
        judge_verdict="veto", judge_reason="test veto",
    )
    defaults.update(kw)
    with Session(eng) as s:
        row = ActionLog(**defaults)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def test_judge_vetoed_row_confirmable_via_endpoint(safety_client, auth_headers):
    eng = safety_client._engine
    aid = _seed_judge_vetoed_action(eng)

    with patch("backend.integrations.obsidian.complete_task", new_callable=AsyncMock, return_value=None) as ct:
        resp = safety_client.post(f"/api/safety/actions/{aid}/confirm", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "executed"
    ct.assert_awaited_once()

    row = _get_log(eng, aid)
    assert row.decision == "executed"
    assert row.judge_verdict == "veto"  # judge verdict is preserved, not overwritten
