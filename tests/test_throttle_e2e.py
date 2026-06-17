"""Broker-throttle end-to-end tests (Tier 3 guardrails).

Drives real execute_action calls against an in-memory StaticPool DB with
autonomy seeded ON.  Tests:

  1. Per-verb rate throttle — N successful agent dispatches then FORBIDDEN
     with error='throttled'; user actor bypasses the cap entirely.
  2. Circuit breaker — after breaker_failure_threshold dispatcher failures the
     breaker trips; throttle.allow() reports circuit_open (or the next
     execute_action returns FORBIDDEN with error='circuit_open').
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import backend.database  # registers all table metadata

from backend.safety import throttle
from backend.safety.broker import Decision, Actor, execute_action


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


def _seed_autonomy_on(eng):
    """Insert SystemState row 1 with autonomy_enabled=True."""
    from backend.database import SystemState
    with Session(eng) as s:
        row = SystemState(id=1, autonomy_enabled=True)
        s.add(row)
        s.commit()


def _make_settings(throttle_max: int, breaker_threshold: int = 10) -> MagicMock:
    """Return a settings mock with the throttle attributes the broker reads."""
    s = MagicMock()
    s.verb_throttle_max = throttle_max
    s.verb_throttle_window_s = 300
    s.breaker_failure_threshold = breaker_threshold
    s.breaker_cooldown_s = 900
    return s


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_throttle_state():
    """Clear all throttle/breaker state before and after every test."""
    throttle.reset()
    yield
    throttle.reset()


@pytest.fixture
def eng(monkeypatch):
    e = make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    _seed_autonomy_on(e)
    return e


# ---------------------------------------------------------------------------
# Test 1 — per-verb rate throttle E2E
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_throttle_e2e_agent_forbidden_after_cap(eng, monkeypatch):
    """N agent dispatches succeed; the (N+1)th is FORBIDDEN with error='throttled'.
    call_service is awaited exactly N times — the throttled call never reaches the
    dispatcher.  A subsequent user action still executes (user bypass)."""
    N = 2
    monkeypatch.setattr("backend.config._settings_instance", _make_settings(throttle_max=N))

    call_service_mock = AsyncMock(return_value={"ok": True})

    with patch("backend.integrations.homeassistant.call_service", call_service_mock), \
         patch("backend.events.notify_phone", new_callable=AsyncMock), \
         patch("backend.events.publish", new_callable=AsyncMock):

        # N calls — each must return EXECUTED (light domain = LOW risk = auto-allowed).
        for i in range(N):
            res = await execute_action(
                actor=Actor.AGENT,
                kind="ha_service",
                target="light.office",
                payload={"domain": "light", "service": "turn_on"},
            )
            assert res.decision == Decision.EXECUTED, (
                f"Call {i+1}: expected EXECUTED, got {res.decision}"
            )

        # Dispatcher called exactly N times so far.
        assert call_service_mock.await_count == N

        # (N+1)th call — must be throttled.
        res_over = await execute_action(
            actor=Actor.AGENT,
            kind="ha_service",
            target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )
        assert res_over.decision == Decision.FORBIDDEN, (
            f"Expected FORBIDDEN for throttled call, got {res_over.decision}"
        )
        assert res_over.error == "throttled", (
            f"Expected error='throttled', got {res_over.error!r}"
        )
        # Dispatcher must NOT have been called again.
        assert call_service_mock.await_count == N, (
            f"call_service called {call_service_mock.await_count} times; expected {N}"
        )

        # User actor is NOT throttled — must still execute even though agent is capped.
        res_user = await execute_action(
            actor=Actor.USER,
            kind="ha_service",
            target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )
        assert res_user.decision == Decision.EXECUTED, (
            f"User action should bypass throttle; got {res_user.decision}"
        )
        # dispatcher was called once more (by the user action).
        assert call_service_mock.await_count == N + 1


# ---------------------------------------------------------------------------
# Test 2 — circuit breaker E2E
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_e2e_trips_after_threshold(eng, monkeypatch):
    """After breaker_failure_threshold agent dispatcher failures the circuit
    breaker trips.  The next throttle.allow() call reports 'circuit_open' (or
    the next execute_action returns FORBIDDEN with error='circuit_open')."""
    THRESHOLD = 2
    # High throttle cap so rate never blocks; low failure threshold to trip fast.
    monkeypatch.setattr(
        "backend.config._settings_instance",
        _make_settings(throttle_max=100, breaker_threshold=THRESHOLD),
    )

    async def boom(target, payload):
        raise RuntimeError("dispatcher exploded on purpose")

    with patch("backend.safety.broker._DISPATCHERS", {"ha_service": boom}), \
         patch("backend.events.notify_phone", new_callable=AsyncMock) as notify_mock, \
         patch("backend.events.publish", new_callable=AsyncMock):

        # Fire THRESHOLD failing agent dispatches.
        for i in range(THRESHOLD):
            res = await execute_action(
                actor=Actor.AGENT,
                kind="ha_service",
                target="light.x",
                payload={"domain": "light", "service": "turn_on"},
            )
            assert res.decision == Decision.FAILED, (
                f"Call {i+1}: expected FAILED (dispatcher raises), got {res.decision}"
            )

    # After THRESHOLD failures the circuit breaker must be tripped.
    # Verify via throttle.allow() directly — circuit_open takes priority.
    ok, reason = throttle.allow("ha_service", max_per_window=100, window_s=300)
    assert ok is False, "Expected throttle.allow() to return False after breaker trips"
    assert reason == "circuit_open", (
        f"Expected reason='circuit_open', got {reason!r}"
    )

    # Also verify that the next execute_action for an agent returns FORBIDDEN
    # (with circuit_open) — proving the broker respects the tripped breaker.
    with patch("backend.safety.broker._DISPATCHERS", {"ha_service": boom}), \
         patch("backend.events.notify_phone", new_callable=AsyncMock), \
         patch("backend.events.publish", new_callable=AsyncMock):
        res_after = await execute_action(
            actor=Actor.AGENT,
            kind="ha_service",
            target="light.x",
            payload={"domain": "light", "service": "turn_on"},
        )
    assert res_after.decision == Decision.FORBIDDEN, (
        f"Expected FORBIDDEN (circuit_open) after breaker trips, got {res_after.decision}"
    )
    assert res_after.error == "circuit_open", (
        f"Expected error='circuit_open', got {res_after.error!r}"
    )

    # Confirm notify_phone was called with kind='circuit_breaker' at least once
    # during the failure loop.
    cb_calls = [
        call for call in notify_mock.call_args_list
        if call.kwargs.get("kind") == "circuit_breaker"
    ]
    assert len(cb_calls) >= 1, (
        f"Expected at least one notify_phone(kind='circuit_breaker'); "
        f"got calls: {notify_mock.call_args_list}"
    )
