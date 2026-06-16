"""Tests for the live notification layer (Tier 3 gate-blocker #5a).

Verifies that:
  1. execute_action with NEEDS_CONFIRM broadcasts exactly one "action" event with the
     correct decision/actor fields.
  2. execute_action that results in EXECUTED broadcasts with decision=="executed".
  3. Kill-switch FORBIDDEN broadcasts with decision=="forbidden".
  4. Idempotency REPLAY does NOT broadcast a second time (only the first real call
     triggers a broadcast).
  5. POST /api/safety/pause and /resume broadcast "autonomy" events with the correct
     enabled value.
  6. events.publish is best-effort: if ws_manager.broadcast raises, publish does NOT
     re-raise.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlmodel.pool import StaticPool
from unittest.mock import AsyncMock, patch

import backend.database  # noqa: F401 — registers all tables on SQLModel.metadata
from backend.safety.broker import (
    Actor,
    Decision,
    Risk,
    Reversibility,
    execute_action,
)


# ---------------------------------------------------------------------------
# Engine fixture — in-memory SQLite, same pattern as test_safety_broker.py
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


def _seed_state(eng, autonomy: bool = True):
    """Seed (or update) the SystemState row so the kill-switch is deterministic."""
    from backend.database import SystemState
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        s.commit()


# ---------------------------------------------------------------------------
# Helper: capture the JSON parsed from the first broadcast call's string arg
# ---------------------------------------------------------------------------

def _broadcast_json(mock_broadcast):
    """Return a list of parsed JSON dicts from all broadcast calls."""
    results = []
    for call in mock_broadcast.call_args_list:
        arg = call.args[0] if call.args else call.kwargs.get("message", "")
        results.append(json.loads(arg))
    return results


# ---------------------------------------------------------------------------
# Test 1: NEEDS_CONFIRM broadcasts exactly one "action" event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_needs_confirm_broadcasts_action_event(eng):
    """An agent action that is queued for confirmation broadcasts decision==needs_confirm."""
    _seed_state(eng, autonomy=True)

    broadcast_mock = AsyncMock()
    with patch("backend.api.agents.ws_manager.broadcast", broadcast_mock), \
         patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock):
        res = await execute_action(
            actor="agent",
            kind="ha_service",
            target="lock.front",
            payload={"domain": "lock", "service": "unlock"},
        )

    assert res.decision == Decision.NEEDS_CONFIRM
    # Exactly one broadcast
    assert broadcast_mock.await_count == 1
    events = _broadcast_json(broadcast_mock)
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "action"
    assert evt["decision"] == "needs_confirm"
    assert evt["actor"] == "agent"
    assert evt["kind"] == "ha_service"
    assert evt["target"] == "lock.front"


# ---------------------------------------------------------------------------
# Test 2: EXECUTED broadcasts with decision=="executed"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_executed_broadcasts_action_event(eng):
    """A user action dispatched successfully broadcasts decision==executed."""
    _seed_state(eng, autonomy=True)

    broadcast_mock = AsyncMock()
    with patch("backend.api.agents.ws_manager.broadcast", broadcast_mock), \
         patch(
             "backend.integrations.homeassistant.call_service",
             new_callable=AsyncMock,
             return_value={"ok": True},
         ):
        res = await execute_action(
            actor="user",
            kind="ha_service",
            target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )

    assert res.decision == Decision.EXECUTED
    assert broadcast_mock.await_count == 1
    events = _broadcast_json(broadcast_mock)
    evt = events[0]
    assert evt["type"] == "action"
    assert evt["decision"] == "executed"
    assert evt["actor"] == "user"


# ---------------------------------------------------------------------------
# Test 3: Kill-switch FORBIDDEN broadcasts with decision=="forbidden"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_kill_switch_forbidden_broadcasts(eng):
    """With autonomy OFF, an agent action is FORBIDDEN and broadcasts decision==forbidden."""
    _seed_state(eng, autonomy=False)

    broadcast_mock = AsyncMock()
    with patch("backend.api.agents.ws_manager.broadcast", broadcast_mock), \
         patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock):
        res = await execute_action(
            actor="agent",
            kind="ha_service",
            target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )

    assert res.decision == Decision.FORBIDDEN
    assert res.error == "autonomy_disabled"
    assert broadcast_mock.await_count == 1
    events = _broadcast_json(broadcast_mock)
    evt = events[0]
    assert evt["type"] == "action"
    assert evt["decision"] == "forbidden"
    assert evt["actor"] == "agent"


# ---------------------------------------------------------------------------
# Test 4: Idempotency REPLAY does NOT trigger a second broadcast
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_idempotency_replay_does_not_broadcast(eng):
    """A replayed action (same idempotency_key, already terminal) must NOT broadcast.

    The first real call triggers exactly one broadcast; the second call (replay)
    returns the recorded result immediately without broadcasting.
    """
    _seed_state(eng, autonomy=True)

    broadcast_mock = AsyncMock()
    with patch("backend.api.agents.ws_manager.broadcast", broadcast_mock), \
         patch(
             "backend.integrations.homeassistant.call_service",
             new_callable=AsyncMock,
             return_value={"ok": 1},
         ):
        res1 = await execute_action(
            actor="user",
            kind="ha_service",
            target="light.office",
            payload={"domain": "light", "service": "turn_on"},
            idempotency_key="idem-test-001",
        )
        res2 = await execute_action(
            actor="user",
            kind="ha_service",
            target="light.office",
            payload={"domain": "light", "service": "turn_on"},
            idempotency_key="idem-test-001",
        )

    assert res1.decision == Decision.EXECUTED
    assert res1.replayed is False
    assert res2.decision == Decision.EXECUTED
    assert res2.replayed is True

    # broadcast called ONCE for the first real call; NOT on the replay
    assert broadcast_mock.await_count == 1, (
        f"Expected 1 broadcast (first real call only), got {broadcast_mock.await_count}"
    )
    events = _broadcast_json(broadcast_mock)
    assert events[0]["decision"] == "executed"


# ---------------------------------------------------------------------------
# Test 5: POST /api/safety/pause and /resume broadcast autonomy events
# ---------------------------------------------------------------------------

@pytest.fixture
def safety_client(tmp_path, monkeypatch):
    """Full-app TestClient with isolated in-memory DB, same pattern as test_safety_broker.py."""
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


def test_pause_broadcasts_autonomy_disabled(safety_client, auth_headers):
    """POST /api/safety/pause broadcasts type=="autonomy" enabled=false."""
    broadcast_mock = AsyncMock()
    with patch("backend.api.agents.ws_manager.broadcast", broadcast_mock):
        resp = safety_client.post("/api/safety/pause", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["autonomy_enabled"] is False

    # Find the autonomy event among any broadcasts
    autonomy_events = [
        e for e in _broadcast_json(broadcast_mock)
        if e.get("type") == "autonomy"
    ]
    assert len(autonomy_events) == 1
    assert autonomy_events[0]["enabled"] is False


def test_resume_broadcasts_autonomy_enabled(safety_client, auth_headers):
    """POST /api/safety/resume broadcasts type=="autonomy" enabled=true."""
    broadcast_mock = AsyncMock()
    with patch("backend.api.agents.ws_manager.broadcast", broadcast_mock):
        resp = safety_client.post("/api/safety/resume", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json()["autonomy_enabled"] is True

    autonomy_events = [
        e for e in _broadcast_json(broadcast_mock)
        if e.get("type") == "autonomy"
    ]
    assert len(autonomy_events) == 1
    assert autonomy_events[0]["enabled"] is True


# ---------------------------------------------------------------------------
# Test 6: events.publish is best-effort — a raising broadcast never propagates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_events_publish_best_effort_swallows_broadcast_error():
    """If ws_manager.broadcast raises, events.publish must NOT re-raise."""
    from backend import events

    async def boom(msg):
        raise RuntimeError("WebSocket exploded")

    with patch("backend.api.agents.ws_manager.broadcast", side_effect=boom):
        # Must not raise
        await events.publish("action", {"decision": "test"})
