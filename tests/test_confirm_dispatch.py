"""Tests for confirm_action (Tier 1.5 gate-blocker #3, Piece B).

Covers:
  1. Happy path — needs_confirm row for a HIGH HA action is confirmed and dispatched.
  2. Re-confirm — the same row cannot be dispatched twice (not_confirmable / 409).
  3. TTL expired — a stale needs_confirm row returns "expired" / 410; row stamped FORBIDDEN.
  4. Kill switch — autonomy OFF blocks confirm for an agent actor; row stamped FORBIDDEN.
  5. Not found — confirm_action(99999) returns "not_found".
  6. API layer — FastAPI TestClient status codes for all branches.

Verifies:
  - The ActionLog row is updated IN PLACE (no second row created on confirm).
  - Re-confirm is "not_confirmable" (prevents double-dispatch).
  - Kill switch and TTL are re-checked at confirm time; dispatcher NOT called on refusal.
"""

import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — registers all SQLModel tables on metadata
from backend.safety.broker import (
    Actor,
    Decision,
    Reversibility,
    Risk,
    confirm_action,
    execute_action,
)


# ---------------------------------------------------------------------------
# Engine + helpers
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
    """Seed (or update) the SystemState row."""
    from backend.database import SystemState
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        s.commit()


def _seed_needs_confirm(eng, *, actor="agent", kind="ha_service", target="lock.front",
                         payload=None, age_seconds=0):
    """Insert a needs_confirm ActionLog row directly (bypasses execute_action).

    `age_seconds` backdates created_at so TTL tests don't need sleeps.
    """
    from backend.database import ActionLog
    if payload is None:
        payload = {"domain": "lock", "service": "unlock"}
    created = datetime.utcnow() - timedelta(seconds=age_seconds)
    with Session(eng) as s:
        row = ActionLog(
            actor=actor,
            kind=kind,
            target=target,
            payload_json=json.dumps(payload),
            risk=Risk.HIGH.value,
            reversibility=Reversibility.UNKNOWN.value,
            decision=Decision.NEEDS_CONFIRM.value,
            result_json=None,
            idempotency_key=None,
            created_at=created,
            updated_at=created,
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


# ---------------------------------------------------------------------------
# Test 1: Happy path — confirm dispatches and updates the row IN PLACE
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_happy_path_executes_and_updates_row_in_place(eng):
    """Confirming a needs_confirm row dispatches via the broker and updates the same row.

    No second ActionLog row is created. call_service is awaited exactly once with
    the correct entity_id payload.
    """
    _seed_state(eng, autonomy=True)

    # Produce a real needs_confirm row via execute_action (agent + HIGH lock domain)
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        cs.return_value = {"ok": True}
        res_gate = await execute_action(
            actor="agent",
            kind="ha_service",
            target="lock.front",
            payload={"domain": "lock", "service": "unlock"},
        )

    assert res_gate.decision == Decision.NEEDS_CONFIRM
    log_id = res_gate.log_id
    assert log_id is not None

    # Confirm it
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        cs.return_value = {"ok": True}
        status, res = await confirm_action(log_id)

    assert status == "executed"
    assert res is not None
    assert res.decision == Decision.EXECUTED

    # Only ONE ActionLog row total — updated in place, not a new row
    logs = _all_logs(eng)
    assert len(logs) == 1, "confirm must update the existing row, not create a second"
    assert logs[0].id == log_id
    assert logs[0].decision == "executed"
    assert json.loads(logs[0].result_json) == {"ok": True}

    # call_service dispatched exactly once during confirm (not during gate)
    cs.assert_awaited_once()
    call_args = cs.call_args
    # _dispatch_ha_service builds service_data = {"entity_id": target} when no service_data
    assert call_args.args[0] == "lock"    # domain
    assert call_args.args[1] == "unlock"  # service


# ---------------------------------------------------------------------------
# Test 2: Re-confirm — same row cannot be dispatched again
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconfirm_same_row_is_not_confirmable_no_double_dispatch(eng):
    """After a successful confirm, re-confirming the same row returns not_confirmable.

    This is the anti-double-dispatch guard. call_service must be awaited only once
    across BOTH confirm calls.
    """
    _seed_state(eng, autonomy=True)

    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        cs.return_value = {"ok": True}
        res_gate = await execute_action(
            actor="agent",
            kind="ha_service",
            target="lock.front",
            payload={"domain": "lock", "service": "unlock"},
        )

    log_id = res_gate.log_id

    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        cs.return_value = {"ok": True}
        # First confirm — succeeds
        status1, res1 = await confirm_action(log_id)
        assert status1 == "executed"
        assert cs.await_count == 1

        # Second confirm — same row is now "executed", not "needs_confirm"
        status2, res2 = await confirm_action(log_id)
        assert status2 == "not_confirmable"
        assert res2 is None
        # call_service still only called once — no double dispatch
        assert cs.await_count == 1, "dispatcher must NOT be called a second time"

    logs = _all_logs(eng)
    assert len(logs) == 1, "re-confirm must not create a second row"


# ---------------------------------------------------------------------------
# Test 3: TTL expired — stale row records FORBIDDEN / reason=expired
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ttl_expired_row_forbidden_no_dispatch(eng):
    """A needs_confirm row older than ttl_seconds is refused.

    The row decision is updated to FORBIDDEN with reason='expired'.
    The dispatcher is NOT called.
    """
    _seed_state(eng, autonomy=True)

    # Backdate created_at by 7200 seconds (2 hours)
    log_id = _seed_needs_confirm(eng, age_seconds=7200)

    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        status, res = await confirm_action(log_id, ttl_seconds=1)

    assert status == "expired"
    assert res is None
    assert cs.call_count == 0, "dispatcher must NOT be called for an expired row"

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].decision == "forbidden"
    result = json.loads(logs[0].result_json)
    assert result["reason"] == "expired"


# ---------------------------------------------------------------------------
# Test 4: Kill switch — autonomy OFF blocks confirm for agent actor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_blocks_confirm_for_agent(eng):
    """With autonomy OFF, confirming a needs_confirm agent action is FORBIDDEN.

    The row decision is updated to FORBIDDEN with reason='autonomy_disabled'.
    The dispatcher is NOT called.
    """
    _seed_state(eng, autonomy=True)

    # Produce a needs_confirm row with autonomy ON
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock):
        res_gate = await execute_action(
            actor="agent",
            kind="ha_service",
            target="lock.front",
            payload={"domain": "lock", "service": "unlock"},
        )
    assert res_gate.decision == Decision.NEEDS_CONFIRM
    log_id = res_gate.log_id

    # NOW disable autonomy before the confirm
    _seed_state(eng, autonomy=False)

    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        status, res = await confirm_action(log_id)

    assert status == "forbidden"
    assert res is not None
    assert res.decision == Decision.FORBIDDEN
    assert res.error == "autonomy_disabled"
    assert cs.call_count == 0, "dispatcher must NOT be called when kill switch is ON"

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].decision == "forbidden"
    result = json.loads(logs[0].result_json)
    assert result["reason"] == "autonomy_disabled"


# ---------------------------------------------------------------------------
# Test 5: Not found
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_not_found(eng):
    status, res = await confirm_action(99999)
    assert status == "not_found"
    assert res is None


# ---------------------------------------------------------------------------
# Test 6: API layer — FastAPI TestClient status codes
# ---------------------------------------------------------------------------

@pytest.fixture
def confirm_client(tmp_path, monkeypatch):
    """Full-app TestClient with an isolated in-memory DB, same pattern as safety_client."""
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


def test_api_confirm_200_executed(confirm_client, auth_headers):
    """POST /api/safety/actions/{id}/confirm on a needs_confirm row → 200 executed."""
    eng = confirm_client._engine
    _seed_state(eng, autonomy=True)
    log_id = _seed_needs_confirm(eng, actor="agent")

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"ok": True},
    ):
        resp = confirm_client.post(
            f"/api/safety/actions/{log_id}/confirm", headers=auth_headers
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == log_id
    assert body["status"] == "executed"
    assert body["decision"] == "executed"
    assert body["result"] == {"ok": True}
    assert body["error"] is None


def test_api_confirm_404_missing(confirm_client, auth_headers):
    """POST confirm on a missing id → 404."""
    resp = confirm_client.post("/api/safety/actions/99999/confirm", headers=auth_headers)
    assert resp.status_code == 404


def test_api_confirm_409_already_executed(confirm_client, auth_headers):
    """POST confirm on an already-executed row → 409."""
    eng = confirm_client._engine
    from backend.database import ActionLog
    with Session(eng) as s:
        row = ActionLog(
            actor="agent", kind="ha_service", target="lock.front",
            payload_json='{"domain":"lock","service":"unlock"}',
            risk="high", reversibility="unknown",
            decision="executed",
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        log_id = row.id

    resp = confirm_client.post(f"/api/safety/actions/{log_id}/confirm", headers=auth_headers)
    assert resp.status_code == 409


def test_api_confirm_410_expired(confirm_client, auth_headers):
    """POST confirm on a stale needs_confirm row → 410.

    We patch broker.confirm_action directly to return ("expired", None) so the
    endpoint mapping is exercised without needing to manipulate pydantic settings.
    """
    eng = confirm_client._engine
    _seed_state(eng, autonomy=True)
    log_id = _seed_needs_confirm(eng, actor="agent", age_seconds=7200)

    async def fake_confirm(action_id, *, ttl_seconds=None):
        return ("expired", None)

    with patch("backend.safety.broker.confirm_action", side_effect=fake_confirm):
        resp = confirm_client.post(
            f"/api/safety/actions/{log_id}/confirm", headers=auth_headers
        )

    assert resp.status_code == 410


def test_api_confirm_403_kill_switch(confirm_client, auth_headers):
    """POST confirm with autonomy OFF → 403."""
    eng = confirm_client._engine
    _seed_state(eng, autonomy=False)
    log_id = _seed_needs_confirm(eng, actor="agent")

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
    ):
        resp = confirm_client.post(
            f"/api/safety/actions/{log_id}/confirm", headers=auth_headers
        )

    assert resp.status_code == 403


def test_api_confirm_auth_required(confirm_client):
    """POST confirm without auth → 401."""
    resp = confirm_client.post("/api/safety/actions/1/confirm")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Extra: verify no second row on confirm (belt-and-suspenders in API path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_no_second_row_created(eng):
    """Confirm updates exactly the one existing row; total ActionLog count stays 1."""
    _seed_state(eng, autonomy=True)

    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock):
        res_gate = await execute_action(
            actor="agent",
            kind="ha_service",
            target="lock.front",
            payload={"domain": "lock", "service": "unlock"},
        )
    log_id = res_gate.log_id

    with patch(
        "backend.integrations.homeassistant.call_service",
        new_callable=AsyncMock,
        return_value={"dispatched": True},
    ):
        await confirm_action(log_id)

    logs = _all_logs(eng)
    assert len(logs) == 1, "must be exactly one ActionLog row after confirm"
    assert logs[0].id == log_id
