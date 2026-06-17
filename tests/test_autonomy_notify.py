"""Tests for the autonomy trust layer: phone notifications + daily digest.

Covers:
  1. notify_phone disabled → returns False, hermes.notify NOT called.
  2. notify_phone enabled → awaits hermes.notify once with correct payload.
  3. notify_phone best-effort: hermes.notify raises → returns False, does NOT raise.
  4a. broker needs_confirm fires a phone alert (kind="needs_confirm").
  4b. EXECUTED action does NOT call notify_phone.
  5. proposer auto-approve fires a phone alert (kind="auto_approved").
  6. build_autonomy_digest produces correct text with seeded DB rows.
  7. Scheduler registers "autonomy_digest" job when autonomy_digest_enabled=True.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

# Register all table metadata before any test runs.
import backend.database  # noqa: F401


# ---------------------------------------------------------------------------
# Shared engine fixture
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
    from backend.database import SystemState
    with Session(eng) as s:
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = autonomy
        s.commit()


# ---------------------------------------------------------------------------
# Test 1: notify_phone disabled → returns False, hermes.notify NOT called
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_phone_disabled_returns_false():
    """When phone_notifications_enabled=False, notify_phone returns False and
    hermes.notify is NOT awaited."""
    hermes_notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.integrations.hermes.notify", hermes_notify_mock):
        s = MagicMock()
        s.phone_notifications_enabled = False
        mock_settings.return_value = s

        from backend.events import notify_phone
        result = await notify_phone("test message", kind="autonomy_alert")

    assert result is False
    hermes_notify_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 2: notify_phone enabled → awaits hermes.notify with correct payload
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_phone_enabled_calls_hermes():
    """When phone_notifications_enabled=True, notify_phone awaits hermes.notify
    exactly once with the correct type and content in the payload."""
    hermes_notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.integrations.hermes.notify", hermes_notify_mock):
        s = MagicMock()
        s.phone_notifications_enabled = True
        mock_settings.return_value = s

        from backend.events import notify_phone
        result = await notify_phone("hello phone", kind="needs_confirm")

    assert result is True
    hermes_notify_mock.assert_awaited_once()
    call_payload = hermes_notify_mock.await_args[0][0]
    assert call_payload["type"] == "needs_confirm"
    assert call_payload["content"] == "hello phone"
    assert "timestamp" in call_payload


# ---------------------------------------------------------------------------
# Test 3: notify_phone best-effort — hermes.notify raises → returns False
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_notify_phone_best_effort_on_hermes_error():
    """If hermes.notify raises, notify_phone must return False and NOT re-raise."""
    with patch("backend.config.get_settings") as mock_settings, \
         patch("backend.integrations.hermes.notify", side_effect=RuntimeError("boom")):
        s = MagicMock()
        s.phone_notifications_enabled = True
        mock_settings.return_value = s

        from backend.events import notify_phone
        result = await notify_phone("test", kind="autonomy_alert")

    assert result is False  # must not raise


# ---------------------------------------------------------------------------
# Test 4a: broker needs_confirm fires a phone alert
# Test 4b: EXECUTED action does NOT call notify_phone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broker_needs_confirm_fires_phone_alert(eng):
    """An agent ha_service on a HIGH domain (lock.front) should result in
    needs_confirm AND fire notify_phone with kind='needs_confirm'."""
    _seed_state(eng, autonomy=True)

    notify_phone_mock = AsyncMock(return_value=True)

    with patch("backend.api.agents.ws_manager.broadcast", AsyncMock()), \
         patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock), \
         patch("backend.events.notify_phone", notify_phone_mock):
        from backend.safety.broker import execute_action, Decision
        res = await execute_action(
            actor="agent",
            kind="ha_service",
            target="lock.front",
            payload={"domain": "lock", "service": "unlock"},
        )

    assert res.decision == Decision.NEEDS_CONFIRM
    notify_phone_mock.assert_awaited_once()
    call_kwargs = notify_phone_mock.await_args
    assert call_kwargs.kwargs.get("kind") == "needs_confirm"
    assert "lock.front" in call_kwargs.args[0]


@pytest.mark.asyncio
async def test_broker_executed_does_not_fire_phone_alert(eng):
    """A user-initiated (always-allowed) light turn_on that EXECUTES must NOT
    call notify_phone — alerts are restricted to needs_confirm only."""
    _seed_state(eng, autonomy=True)

    notify_phone_mock = AsyncMock(return_value=True)

    with patch("backend.api.agents.ws_manager.broadcast", AsyncMock()), \
         patch(
             "backend.integrations.homeassistant.call_service",
             new_callable=AsyncMock,
             return_value={"ok": True},
         ), \
         patch("backend.events.notify_phone", notify_phone_mock):
        from backend.safety.broker import execute_action, Decision
        res = await execute_action(
            actor="user",
            kind="ha_service",
            target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )

    assert res.decision == Decision.EXECUTED
    notify_phone_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_broker_forbidden_does_not_fire_phone_alert(eng):
    """A FORBIDDEN agent action (autonomy disabled) must NOT call notify_phone."""
    _seed_state(eng, autonomy=False)

    notify_phone_mock = AsyncMock(return_value=True)

    with patch("backend.api.agents.ws_manager.broadcast", AsyncMock()), \
         patch("backend.integrations.homeassistant.call_service", new_callable=AsyncMock), \
         patch("backend.events.notify_phone", notify_phone_mock):
        from backend.safety.broker import execute_action, Decision
        res = await execute_action(
            actor="agent",
            kind="ha_service",
            target="light.office",
            payload={"domain": "light", "service": "turn_on"},
        )

    assert res.decision == Decision.FORBIDDEN
    notify_phone_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# Test 5: proposer auto-approve fires a phone alert (kind="auto_approved")
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_proposer_auto_approve_fires_phone_alert(eng, monkeypatch):
    """With auto_approve_low_risk=True and a low+reversible autonomous goal,
    the proposer should fire notify_phone with kind='auto_approved' exactly once."""
    _seed_state(eng, autonomy=True)

    # Patch integrations.
    fake = SimpleNamespace(
        entities=[], alerts=[], docker_containers=[], array_status="started",
        storage_used_gb=1.0, storage_total_gb=10.0, recording_now=[],
        blocked_today=0, blocked_pct=0.0, filtering_enabled=True,
        summary="Clear, 70F",
    )
    for mod_path in (
        "backend.integrations.homeassistant.fetch",
        "backend.integrations.unraid.fetch",
        "backend.integrations.channels_dvr.fetch",
        "backend.integrations.adguard.fetch",
        "backend.integrations.weather.fetch",
    ):
        monkeypatch.setattr(mod_path, AsyncMock(return_value=fake))

    # Patch get_pool so goals.approve() doesn't really run a task.
    pool_mock = MagicMock()
    pool_mock.enqueue = AsyncMock()
    monkeypatch.setattr("backend.agents.goals.get_pool", lambda: pool_mock)

    notify_phone_mock = AsyncMock(return_value=True)
    opus_response = json.dumps([
        {
            "title": "Archive old recordings",
            "description": "Move Channels DVR recordings older than 90 days to cold storage.",
            "risk": "low",
            "reversibility": "reversible",
            "confidence": 0.9,
        }
    ])

    with patch("backend.agents.router.opus", new=AsyncMock(return_value=opus_response)), \
         patch("backend.config.get_settings") as mock_settings, \
         patch("backend.events.notify_phone", notify_phone_mock):
        s = MagicMock()
        s.proposer_max_per_tick = 3
        s.goal_ttl_seconds = 86400
        s.goal_debounce_seconds = 3600
        s.auto_approve_low_risk = True
        mock_settings.return_value = s

        from backend.agents.proposer import propose_goals_tick
        result = await propose_goals_tick()

    assert result["status"] == "ok"
    assert result["count_auto_approved"] == 1

    notify_phone_mock.assert_awaited_once()
    call_kwargs = notify_phone_mock.await_args
    assert call_kwargs.kwargs.get("kind") == "auto_approved"
    assert "Archive old recordings" in call_kwargs.args[0]


# ---------------------------------------------------------------------------
# Test 6: build_autonomy_digest produces correct text
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_build_autonomy_digest_text(eng):
    """Seed an auto-approved goal, a proposed goal, a needs_confirm ActionLog,
    and a SpendLog. Verify the digest text contains all expected elements."""
    from backend.database import Goal, ActionLog, SpendLog, SystemState

    with Session(eng) as s:
        # SystemState
        row = s.get(SystemState, 1)
        if row is None:
            row = SystemState(id=1)
            s.add(row)
        row.autonomy_enabled = True
        row.daily_budget_usd = 25.0

        # Auto-approved goal (recent)
        auto_goal = Goal(
            title="Archive old recordings",
            description="Move recordings to cold storage.",
            actor="autonomous",
            status="running",
            risk="low",
            reversibility="reversible",
            approved_by="auto:low_risk_reversible",
            updated_at=datetime.utcnow(),
        )
        s.add(auto_goal)

        # Proposed goal (awaiting human)
        proposed_goal = Goal(
            title="Clean up Docker images",
            description="Run docker prune.",
            actor="autonomous",
            status="proposed",
            risk="low",
            reversibility="reversible",
        )
        s.add(proposed_goal)

        # needs_confirm action log
        action_log = ActionLog(
            actor="agent",
            kind="ha_service",
            target="lock.front",
            payload_json='{"domain":"lock","service":"unlock"}',
            risk="high",
            reversibility="unknown",
            decision="needs_confirm",
        )
        s.add(action_log)

        # SpendLog entry
        spend = SpendLog(
            model="claude-sonnet-4-6",
            cost_usd=1.23,
            created_at=datetime.utcnow(),
        )
        s.add(spend)

        s.commit()

    from backend.agents.digest import build_autonomy_digest
    text = await build_autonomy_digest()

    # Must contain the auto-ran goal title
    assert "Archive old recordings" in text, f"Missing auto-ran goal in digest: {text}"
    # Must contain the proposed goal title
    assert "Clean up Docker images" in text, f"Missing proposed goal in digest: {text}"
    # Must contain pending count (1 needs_confirm action)
    assert "1 action" in text, f"Missing pending action count in digest: {text}"
    # Must contain spend line
    assert "$" in text, f"Missing spend figure in digest: {text}"
    assert "1.23" in text or "1.2" in text, f"Missing spend value in digest: {text}"


# ---------------------------------------------------------------------------
# Test 7: Scheduler registers "autonomy_digest" job when enabled
# ---------------------------------------------------------------------------

def test_scheduler_registers_autonomy_digest_job():
    """With autonomy_digest_enabled=True (the default), setup_scheduler should
    register a job with id='autonomy_digest'."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add, \
         patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.proposer_enabled = True
        s.proposer_interval_hours = 6
        s.autonomy_digest_enabled = True
        s.autonomy_digest_time = "20:00"
        mock_settings.return_value = s

        setup_scheduler("07:00", "America/Detroit")

    ids_added = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "autonomy_digest" in ids_added, (
        f"Expected 'autonomy_digest' in scheduler jobs; got: {ids_added}"
    )


def test_scheduler_no_digest_job_when_disabled():
    """With autonomy_digest_enabled=False, setup_scheduler should NOT register
    the 'autonomy_digest' job."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add, \
         patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.proposer_enabled = False
        s.autonomy_digest_enabled = False
        mock_settings.return_value = s

        setup_scheduler("07:00", "America/Detroit")

    ids_added = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "autonomy_digest" not in ids_added, (
        f"'autonomy_digest' should NOT be registered when disabled; got: {ids_added}"
    )


def test_scheduler_digest_invalid_time_falls_back():
    """A malformed autonomy_digest_time falls back to 20:00 without crashing."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add, \
         patch("backend.config.get_settings") as mock_settings:
        s = MagicMock()
        s.proposer_enabled = False
        s.autonomy_digest_enabled = True
        s.autonomy_digest_time = "NOT_A_TIME"
        mock_settings.return_value = s

        # Must not raise.
        setup_scheduler("07:00", "America/Detroit")

    ids_added = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "autonomy_digest" in ids_added, (
        f"'autonomy_digest' should still be registered after fallback; got: {ids_added}"
    )
