"""Tests for backend/safety/throttle.py (per-verb rate throttle + circuit breaker)
and the broker wiring that gates agent/autonomous dispatches.

Design rules:
  - throttle.reset() at the start of every test (or autouse fixture) so module-level
    state never leaks between tests.
  - All timing-sensitive assertions use explicit `now=` so tests are deterministic
    and never sleep.
  - User actions are NEVER throttled — broker must pass them through unconditionally.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — registers all table metadata

from backend.safety import throttle


# ---------------------------------------------------------------------------
# Autouse reset — clears module-level _STATE before every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_throttle():
    throttle.reset()
    yield
    throttle.reset()


# ---------------------------------------------------------------------------
# Shared DB engine helper (mirrors test_safety_broker.py pattern)
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


# ---------------------------------------------------------------------------
# 1. throttle.allow — rate-cap enforcement
# ---------------------------------------------------------------------------

def test_allow_permits_up_to_max():
    """Dispatches 0..max-1 are all allowed; the max-th is throttled."""
    for _ in range(2):
        throttle.record_attempt("ha_service")
    ok, reason = throttle.allow("ha_service", max_per_window=2, window_s=300)
    assert ok is False
    assert reason == "throttled"


def test_allow_window_expiry_resets_count():
    """Attempts older than window_s are pruned; allow() returns True after the window."""
    base = 0.0
    # Record two attempts at t=0
    throttle.record_attempt("ha_service", now=base)
    throttle.record_attempt("ha_service", now=base)

    # At t=0 we're at the cap (max=2) — throttled
    ok, reason = throttle.allow("ha_service", max_per_window=2, window_s=300, now=base)
    assert ok is False
    assert reason == "throttled"

    # At t=base+301 the attempts have expired — allowed again
    future = base + 301.0
    ok, reason = throttle.allow("ha_service", max_per_window=2, window_s=300, now=future)
    assert ok is True
    assert reason is None


def test_allow_returns_true_below_cap():
    """No attempts recorded yet → always allowed."""
    ok, reason = throttle.allow("new_kind", max_per_window=5, window_s=300)
    assert ok is True
    assert reason is None


# ---------------------------------------------------------------------------
# 2. throttle.record_result — circuit breaker trip
# ---------------------------------------------------------------------------

def test_circuit_breaker_trips_at_threshold():
    """After failure_threshold failures, record_result returns True (tripped)."""
    base = 0.0
    results = []
    for i in range(3):
        tripped = throttle.record_result(
            "ha_service", False,
            failure_threshold=3,
            window_s=300,
            cooldown_s=900,
            now=base + i,
        )
        results.append(tripped)
    # Only the 3rd call (reaching threshold) trips the breaker.
    assert results == [False, False, True]


def test_circuit_open_blocks_allow():
    """After the breaker trips, allow() returns (False, 'circuit_open')."""
    base = 0.0
    for i in range(3):
        throttle.record_result(
            "ha_service", False,
            failure_threshold=3,
            window_s=300,
            cooldown_s=900,
            now=base + i,
        )
    # Immediately after trip — circuit is open.
    ok, reason = throttle.allow("ha_service", max_per_window=5, window_s=300, now=base + 3)
    assert ok is False
    assert reason == "circuit_open"


def test_circuit_opens_until_cooldown_expires():
    """Breaker is open until now > trip_until; after cooldown allow() succeeds."""
    base = 0.0
    for i in range(3):
        throttle.record_result(
            "ha_service", False,
            failure_threshold=3,
            window_s=300,
            cooldown_s=900,
            now=base + i,
        )
    # The trip was at base+2 (the third failure); trip_until = base+2+900 = base+902.
    still_cooling = base + 901.0
    ok, _ = throttle.allow("ha_service", max_per_window=5, window_s=300, now=still_cooling)
    assert ok is False

    after_cooldown = base + 903.0
    ok, reason = throttle.allow("ha_service", max_per_window=5, window_s=300, now=after_cooldown)
    assert ok is True
    assert reason is None


def test_success_resets_failure_streak():
    """A success clears the failure list; subsequent failures restart the countdown."""
    base = 0.0
    # Two failures — not yet at threshold of 3.
    throttle.record_result(
        "ha_service", False,
        failure_threshold=3, window_s=300, cooldown_s=900, now=base,
    )
    throttle.record_result(
        "ha_service", False,
        failure_threshold=3, window_s=300, cooldown_s=900, now=base + 1,
    )
    # A success should reset the streak.
    throttle.record_result(
        "ha_service", True,
        failure_threshold=3, window_s=300, cooldown_s=900, now=base + 2,
    )
    # One more failure — only 1 in streak now, should NOT trip.
    tripped = throttle.record_result(
        "ha_service", False,
        failure_threshold=3, window_s=300, cooldown_s=900, now=base + 3,
    )
    assert tripped is False
    # allow() should still be open (no trip).
    ok, _ = throttle.allow("ha_service", max_per_window=10, window_s=300, now=base + 4)
    assert ok is True


# ---------------------------------------------------------------------------
# 3. Broker — USER actions are NEVER throttled
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_user_never_throttled(eng, monkeypatch):
    """Many USER dispatches all execute; throttle is never consulted for users."""
    from backend.safety.broker import execute_action, Decision

    # Lower the cap to 1 so any agent would immediately be throttled after the first call.
    monkeypatch.setattr(throttle, "_STATE", {})  # ensure clean state
    s = MagicMock()
    s.verb_throttle_max = 1
    s.verb_throttle_window_s = 300
    s.breaker_failure_threshold = 3
    s.breaker_cooldown_s = 900
    monkeypatch.setattr("backend.config._settings_instance", s)

    call_count = 0

    async def fake_call_service(domain, service, data):
        nonlocal call_count
        call_count += 1
        return {"ok": True}

    # Fire 5 user dispatches — all should execute.
    with patch("backend.integrations.homeassistant.call_service", side_effect=fake_call_service):
        for _ in range(5):
            res = await execute_action(
                actor="user", kind="ha_service", target="light.office",
                payload={"domain": "light", "service": "turn_on"},
            )
            assert res.decision == Decision.EXECUTED, f"Expected EXECUTED, got {res.decision}"

    assert call_count == 5


# ---------------------------------------------------------------------------
# 4. Broker — AGENT action over cap → FORBIDDEN with reason "throttled"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_throttled_after_cap(eng, monkeypatch):
    """An AGENT action that exceeds verb_throttle_max is FORBIDDEN; dispatcher not called."""
    from backend.safety.broker import execute_action, Decision, Actor

    # Set cap to 1 via settings mock.
    s = MagicMock()
    s.verb_throttle_max = 1
    s.verb_throttle_window_s = 300
    s.breaker_failure_threshold = 3
    s.breaker_cooldown_s = 900
    monkeypatch.setattr("backend.config._settings_instance", s)

    # Seed autonomy ON so the kill-switch doesn't block.
    from backend.database import SystemState
    with Session(eng) as session:
        row = SystemState(id=1, autonomy_enabled=True)
        session.add(row)
        session.commit()

    call_count = 0

    async def fake_call_service(domain, service, data):
        nonlocal call_count
        call_count += 1
        return {"ok": True}

    with patch("backend.integrations.homeassistant.call_service", side_effect=fake_call_service), \
         patch("backend.events.notify_phone", new_callable=AsyncMock):
        # First call — below the cap, should execute.
        res1 = await execute_action(
            actor=Actor.AGENT, kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )
        assert res1.decision == Decision.EXECUTED
        assert call_count == 1

        # Second call — at/over the cap, should be FORBIDDEN (throttled).
        res2 = await execute_action(
            actor=Actor.AGENT, kind="ha_service", target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )
        assert res2.decision == Decision.FORBIDDEN
        assert res2.error == "throttled"
        # Dispatcher must NOT have been called a second time.
        assert call_count == 1


# ---------------------------------------------------------------------------
# 5. Broker — repeated AGENT failures trip the breaker + notify_phone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_failures_trip_breaker_and_alert(eng, monkeypatch):
    """After breaker_failure_threshold failures, breaker trips and notify_phone
    is called with kind='circuit_breaker'."""
    from backend.safety.broker import execute_action, Decision, Actor

    # High cap so rate never blocks; low failure threshold to trip quickly.
    s = MagicMock()
    s.verb_throttle_max = 100
    s.verb_throttle_window_s = 300
    s.breaker_failure_threshold = 3
    s.breaker_cooldown_s = 900
    monkeypatch.setattr("backend.config._settings_instance", s)

    # Seed autonomy ON.
    from backend.database import SystemState
    with Session(eng) as session:
        row = SystemState(id=1, autonomy_enabled=True)
        session.add(row)
        session.commit()

    async def boom(target, payload):
        raise RuntimeError("dispatcher exploded")

    notify_mock = AsyncMock(return_value=True)
    with patch("backend.safety.broker._DISPATCHERS", {"ha_service": boom}), \
         patch("backend.events.notify_phone", notify_mock):
        for _ in range(3):
            res = await execute_action(
                actor=Actor.AGENT, kind="ha_service", target="light.x",
                payload={"domain": "light", "service": "turn_on"},
            )
            assert res.decision == Decision.FAILED

    # notify_phone must have been called with kind="circuit_breaker" at least once.
    cb_calls = [
        call for call in notify_mock.call_args_list
        if call.kwargs.get("kind") == "circuit_breaker"
    ]
    assert len(cb_calls) >= 1, (
        f"Expected at least one notify_phone(kind='circuit_breaker') call; "
        f"got: {notify_mock.call_args_list}"
    )


# ---------------------------------------------------------------------------
# 6. Goals — auto-approved failure triggers alert; user-approved does not
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconcile_auto_approved_failure_alerts(eng, monkeypatch):
    """A running goal whose Task is 'failed' AND approved_by starts with 'auto:'
    triggers events.notify_phone with kind='goal_failed'."""
    from backend.agents import goals
    from backend.database import Goal, Task

    # Seed a task in 'failed' status.
    with Session(eng) as s:
        t = Task(prompt="do thing", status="failed")
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id

        g = Goal(
            title="Auto prune images",
            description="Prune stale Docker images.",
            status="running",
            fingerprint="auto0000auto0000",
            task_id=task_id,
            approved_by="auto:low_risk_reversible",
            attempts=0,
        )
        s.add(g)
        s.commit()

    notify_mock = AsyncMock(return_value=True)
    with patch("backend.events.notify_phone", notify_mock):
        await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)

    # Must have called notify_phone with kind="goal_failed".
    goal_failed_calls = [
        call for call in notify_mock.call_args_list
        if call.kwargs.get("kind") == "goal_failed"
    ]
    assert len(goal_failed_calls) == 1
    assert "Auto prune images" in goal_failed_calls[0].args[0]


@pytest.mark.asyncio
async def test_reconcile_user_approved_failure_no_alert(eng, monkeypatch):
    """A running goal whose Task is 'failed' AND approved_by='user' does NOT
    trigger the goal_failed phone alert."""
    from backend.agents import goals
    from backend.database import Goal, Task

    with Session(eng) as s:
        t = Task(prompt="do user thing", status="failed")
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id

        g = Goal(
            title="User-approved task",
            description="Something the user approved.",
            status="running",
            fingerprint="user0000user0000",
            task_id=task_id,
            approved_by="user",
            attempts=0,
        )
        s.add(g)
        s.commit()

    notify_mock = AsyncMock(return_value=True)
    with patch("backend.events.notify_phone", notify_mock):
        await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)

    # goal_failed must NOT have been called.
    goal_failed_calls = [
        call for call in notify_mock.call_args_list
        if call.kwargs.get("kind") == "goal_failed"
    ]
    assert len(goal_failed_calls) == 0


@pytest.mark.asyncio
async def test_reconcile_no_approved_by_no_alert(eng, monkeypatch):
    """A running goal with no approved_by field (None / empty) does NOT alert."""
    from backend.agents import goals
    from backend.database import Goal, Task

    with Session(eng) as s:
        t = Task(prompt="do something", status="failed")
        s.add(t)
        s.commit()
        s.refresh(t)
        task_id = t.id

        g = Goal(
            title="No approver goal",
            description="Goal with no approved_by.",
            status="running",
            fingerprint="none0000none0000",
            task_id=task_id,
            approved_by=None,
            attempts=0,
        )
        s.add(g)
        s.commit()

    notify_mock = AsyncMock(return_value=True)
    with patch("backend.events.notify_phone", notify_mock):
        await goals.reconcile_running(backoff_base_seconds=300, max_attempts=5)

    goal_failed_calls = [
        call for call in notify_mock.call_args_list
        if call.kwargs.get("kind") == "goal_failed"
    ]
    assert len(goal_failed_calls) == 0
