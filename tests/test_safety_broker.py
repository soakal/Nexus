import json

import pytest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Ensure all tables (incl. ActionLog) are registered on SQLModel.metadata.
import backend.database  # noqa: F401,E402
from backend.safety.broker import (
    Actor,
    Decision,
    Reversibility,
    Risk,
    classify,
    decide,
    execute_action,
)


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


# ---------------------------------------------------------------------------
# classify — pure
# ---------------------------------------------------------------------------

def test_classify_ha_low_domain():
    assert classify("ha_service", {"domain": "light"}) == (Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE)
    assert classify("ha_service", {"domain": "switch"}) == (Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE)
    assert classify("ha_service", {"domain": "fan"}) == (Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE)
    assert classify("ha_service", {"domain": "input_boolean"}) == (Risk.LOW, Reversibility.REVERSIBLE_BY_INVERSE)


def test_classify_ha_high_domain():
    for d in ("lock", "cover", "climate", "alarm_control_panel"):
        assert classify("ha_service", {"domain": d}) == (Risk.HIGH, Reversibility.UNKNOWN)


def test_classify_ha_other_or_missing_domain_is_medium():
    assert classify("ha_service", {"domain": "media_player"}) == (Risk.MEDIUM, Reversibility.UNKNOWN)
    assert classify("ha_service", {}) == (Risk.MEDIUM, Reversibility.UNKNOWN)


def test_classify_hermes_relay_is_high():
    assert classify("hermes_relay", {"message": "restart jellyfin"}) == (Risk.HIGH, Reversibility.UNKNOWN)


def test_classify_unknown_kind_is_unclassifiable():
    assert classify("totally_new_thing", {}) == (Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN)


# ---------------------------------------------------------------------------
# decide — pure
# ---------------------------------------------------------------------------

def test_decide_user_high_allowed():  # AC3.2
    assert decide(Actor.USER, Risk.HIGH, Reversibility.UNKNOWN, confirmed=False) == Decision.ALLOWED


def test_decide_agent_high_needs_confirm():  # AC3.3
    assert decide(Actor.AGENT, Risk.HIGH, Reversibility.UNKNOWN, confirmed=False) == Decision.NEEDS_CONFIRM


def test_decide_agent_irreversible_forbidden():  # AC3.4
    assert decide(Actor.AGENT, Risk.LOW, Reversibility.IRREVERSIBLE, confirmed=False) == Decision.FORBIDDEN


def test_decide_agent_high_confirmed_allowed():  # AC3.5
    assert decide(Actor.AGENT, Risk.HIGH, Reversibility.UNKNOWN, confirmed=True) == Decision.ALLOWED


def test_decide_agent_irreversible_confirmed_allowed():
    assert decide(Actor.AGENT, Risk.HIGH, Reversibility.IRREVERSIBLE, confirmed=True) == Decision.ALLOWED


def test_decide_agent_medium_allowed():
    assert decide(Actor.AGENT, Risk.MEDIUM, Reversibility.UNKNOWN, confirmed=False) == Decision.ALLOWED


def test_decide_autonomous_unclassifiable_needs_confirm():
    assert decide(Actor.AUTONOMOUS, Risk.UNCLASSIFIABLE, Reversibility.UNKNOWN, confirmed=False) == Decision.NEEDS_CONFIRM


# ---------------------------------------------------------------------------
# execute_action — behaviour
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_high_action_allowed_logged_executed(eng):
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock, return_value={"ok": True}) as cs:
        res = await execute_action(
            actor="user", kind="ha_service", target="lock.front_door",
            payload={"domain": "lock", "service": "unlock"},
        )
    assert res.decision == Decision.EXECUTED
    assert res.risk == Risk.HIGH
    assert res.result == {"ok": True}
    cs.assert_awaited_once()

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].decision == "executed"
    assert logs[0].actor == "user"
    assert logs[0].kind == "ha_service"


@pytest.mark.asyncio
async def test_agent_high_action_needs_confirm_no_dispatch(eng):
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        res = await execute_action(
            actor="agent", kind="ha_service", target="lock.front_door",
            payload={"domain": "lock", "service": "unlock"},
        )
    assert res.decision == Decision.NEEDS_CONFIRM
    assert cs.call_count == 0

    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].decision == "needs_confirm"


@pytest.mark.asyncio
async def test_agent_irreversible_forbidden(eng):
    # No production kind yields IRREVERSIBLE today; assert the gate forbids it and
    # never dispatches by driving decide() through execute_action with a patched
    # classify that returns IRREVERSIBLE.
    with patch("backend.safety.broker.classify", return_value=(Risk.LOW, Reversibility.IRREVERSIBLE)), \
         patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        res = await execute_action(
            actor="agent", kind="ha_service", target="x.y",
            payload={"domain": "light", "service": "turn_on"},
        )
    assert res.decision == Decision.FORBIDDEN
    assert cs.call_count == 0
    logs = _all_logs(eng)
    assert len(logs) == 1
    assert logs[0].decision == "forbidden"


@pytest.mark.asyncio
async def test_idempotency_replay_does_not_redispatch(eng):
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock, return_value={"ok": 1}) as cs:
        res1 = await execute_action(
            actor="user", kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
            idempotency_key="abc123",
        )
        res2 = await execute_action(
            actor="user", kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
            idempotency_key="abc123",
        )
    assert res1.decision == Decision.EXECUTED
    assert res1.replayed is False
    assert res2.decision == Decision.EXECUTED
    assert res2.replayed is True
    assert res2.result == {"ok": 1}
    assert cs.call_count == 1

    logs = _all_logs(eng)
    assert len(logs) == 1  # second call did not insert a new row


@pytest.mark.asyncio
async def test_action_log_written_before_and_after(eng):
    """The intent row is written BEFORE dispatch (visible from inside the
    dispatcher) and UPDATEd to the final state AFTER."""
    from backend.database import ActionLog

    seen = {}

    async def fake_call_service(domain, service, data):
        # Mid-dispatch: the BEFORE row already exists with the gate decision.
        with Session(eng) as s:
            rows = s.exec(select(ActionLog)).all()
            seen["count_during"] = len(rows)
            seen["decision_during"] = rows[0].decision
        return {"done": True}

    with patch("backend.integrations.homeassistant.call_service", side_effect=fake_call_service):
        res = await execute_action(
            actor="user", kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )

    assert seen["count_during"] == 1
    assert seen["decision_during"] == "allowed"  # BEFORE write holds the gate outcome
    assert res.decision == Decision.EXECUTED
    logs = _all_logs(eng)
    assert logs[0].decision == "executed"        # AFTER write holds the dispatch outcome
    assert json.loads(logs[0].result_json) == {"done": True}


@pytest.mark.asyncio
async def test_dispatch_failure_records_failed(eng):
    async def boom(domain, service, data):
        raise RuntimeError("HA exploded")

    with patch("backend.integrations.homeassistant.call_service", side_effect=boom):
        res = await execute_action(
            actor="user", kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )
    # No exception escaped.
    assert res.decision == Decision.FAILED
    assert "HA exploded" in res.error
    logs = _all_logs(eng)
    assert logs[0].decision == "failed"
    assert json.loads(logs[0].result_json)["error"] == "HA exploded"


@pytest.mark.asyncio
async def test_no_dispatcher_for_kind_records_failed(eng):
    # An unknown kind for a USER (always allowed) with no dispatcher -> failed.
    res = await execute_action(
        actor="user", kind="mystery_kind", target="t", payload={},
    )
    assert res.decision == Decision.FAILED
    assert "no dispatcher" in res.error
    logs = _all_logs(eng)
    assert logs[0].decision == "failed"


@pytest.mark.asyncio
async def test_unknown_actor_string_degrades_to_autonomous(eng):
    # Unknown actor + HIGH risk -> autonomous policy -> needs_confirm, never allowed.
    with patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock) as cs:
        res = await execute_action(
            actor="some_random_actor", kind="ha_service", target="lock.x",
            payload={"domain": "lock", "service": "unlock"},
        )
    assert res.decision == Decision.NEEDS_CONFIRM
    assert cs.call_count == 0
    logs = _all_logs(eng)
    assert logs[0].actor == "autonomous"


# ---------------------------------------------------------------------------
# chat routing through the broker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_home_control_routes_through_broker(eng):
    from types import SimpleNamespace

    from backend.agents import chat as chat_mod

    intent_json = json.dumps({"intent": "HOME_CONTROL", "reason": "x"})
    pick_json = json.dumps({"entity_id": "light.office", "service": "turn_on"})

    async def fake_haiku(prompt, *a, **k):
        if "Classify this user message" in prompt:
            return intent_json
        return pick_json

    ha_data = SimpleNamespace(entities=[
        {"entity_id": "light.office", "state": "off", "attributes": {"friendly_name": "Office Light"}},
    ])

    with patch("backend.agents.router.haiku", new=fake_haiku), \
         patch("backend.integrations.homeassistant.fetch", new_callable=AsyncMock, return_value=ha_data), \
         patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock, return_value={"ok": True}) as cs:
        out = await chat_mod.chat(None, "turn on the office light")

    assert "Turned on Office Light" in out["reply"]
    cs.assert_awaited_once()

    logs = _all_logs(eng)
    action_logs = [l for l in logs if l.kind == "ha_service"]
    assert len(action_logs) == 1
    assert action_logs[0].decision == "executed"
    assert action_logs[0].kind == "ha_service"


@pytest.mark.asyncio
async def test_chat_hermes_routes_through_broker(eng):
    from backend.agents import chat as chat_mod

    intent_json = json.dumps({"intent": "HERMES", "reason": "x"})

    async def fake_haiku(prompt, *a, **k):
        return intent_json

    with patch("backend.agents.router.haiku", new=fake_haiku), \
         patch("backend.integrations.hermes.relay", new_callable=AsyncMock, return_value="ok done") as rl:
        out = await chat_mod.chat(None, "Hermes restart jellyfin")

    assert out["reply"] == "ok done"
    rl.assert_awaited_once()

    logs = _all_logs(eng)
    action_logs = [l for l in logs if l.kind == "hermes_relay"]
    assert len(action_logs) == 1
    assert action_logs[0].decision == "executed"
    assert action_logs[0].risk == "high"


# ---------------------------------------------------------------------------
# /api/safety/actions endpoint
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


def _seed_action(eng, **kw):
    from backend.database import ActionLog

    defaults = dict(
        actor="user", kind="ha_service", target="light.office",
        payload_json="{}", risk="low", reversibility="reversible_by_inverse",
        decision="executed", result_json=None, idempotency_key=None,
    )
    defaults.update(kw)
    with Session(eng) as s:
        row = ActionLog(**defaults)
        s.add(row)
        s.commit()
        s.refresh(row)
        return row.id


def test_safety_actions_endpoint_auth_and_list(safety_client, auth_headers):
    eng = safety_client._engine
    _seed_action(eng, target="light.a", decision="executed")
    _seed_action(eng, target="light.b", decision="failed")

    # 401 without a key
    resp = safety_client.get("/api/safety/actions")
    assert resp.status_code == 401

    # 200 with key, newest-first
    resp = safety_client.get("/api/safety/actions", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["target"] == "light.b"  # newest first
    assert data[1]["target"] == "light.a"

    # ?decision= filter
    resp = safety_client.get("/api/safety/actions?decision=failed", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["decision"] == "failed"


def test_safety_confirm_404_and_409(safety_client, auth_headers):
    eng = safety_client._engine
    # missing -> 404
    resp = safety_client.post("/api/safety/actions/9999/confirm", headers=auth_headers)
    assert resp.status_code == 404

    # not-awaiting-confirmation -> 409
    aid = _seed_action(eng, decision="executed")
    resp = safety_client.post(f"/api/safety/actions/{aid}/confirm", headers=auth_headers)
    assert resp.status_code == 409

    # needs_confirm -> documented inert response
    aid2 = _seed_action(eng, decision="needs_confirm")
    resp = safety_client.post(f"/api/safety/actions/{aid2}/confirm", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirm_not_yet_wired"
