"""Tests for backend/agents/watchdog.py — scheduler stall watchdog + dead-letter alert.

Safety contract being verified:
1. check_scheduler_stalls returns overdue job ids and alerts for them (kind="scheduler_stall").
2. The watchdog's own job id ("watchdog") is always skipped — no self-alert.
3. On-time jobs are never flagged or alerted.
4. Debounce: the same stalled job only triggers one alert per cooldown window.
5. check_dead_letters alerts when rows >= threshold, ignores rows below threshold.
6. Dead-letter debounce: second call within cooldown does not re-alert.
7. run_watchdog returns {"skipped": True} when watchdog_enabled=False.
8. Best-effort: scheduler.get_jobs() raising does not propagate — returns [].
9. run_watchdog always returns a dict and never raises.
10. Scheduler registers job id "watchdog" when watchdog_enabled=True.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

import backend.database  # noqa: F401 — register all models on metadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture
def eng(monkeypatch):
    e = _make_engine()
    monkeypatch.setattr("backend.database.engine", e)
    return e


def _fake_job(job_id: str, next_run_time):
    """Return a minimal fake APScheduler Job object."""
    return SimpleNamespace(id=job_id, next_run_time=next_run_time)


def _utcnow():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Reset debounce state before every test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_watchdog():
    from backend.agents import watchdog
    watchdog.reset()
    yield
    watchdog.reset()


# ---------------------------------------------------------------------------
# Test 1 — check_scheduler_stalls detects overdue job, skips on-time + self
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_scheduler_stalls_overdue_alerted():
    """An overdue job triggers an alert; on-time and self (watchdog) do not."""
    from backend.agents import watchdog

    now_utc = _utcnow()
    grace_s = 300  # 5 minutes grace
    cooldown_s = 3600

    overdue_job = _fake_job("morning_briefing", now_utc - timedelta(seconds=grace_s + 60))
    ontime_job = _fake_job("trend_snapshots", now_utc + timedelta(seconds=60))
    self_job = _fake_job("watchdog", now_utc - timedelta(seconds=grace_s * 2))  # overdue but is self

    fake_scheduler = SimpleNamespace(get_jobs=lambda: [overdue_job, ontime_job, self_job])

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.scheduler.scheduler", fake_scheduler), \
         patch("backend.events.notify_phone", notify_mock):
        result = await watchdog.check_scheduler_stalls(grace_s=grace_s, cooldown_s=cooldown_s)

    # Only the overdue non-self job is returned
    assert result == ["morning_briefing"]

    # Exactly one alert fired, for the overdue job, with correct kind
    notify_mock.assert_called_once()
    call_kwargs = notify_mock.call_args.kwargs
    assert call_kwargs["kind"] == "scheduler_stall"
    assert "morning_briefing" in notify_mock.call_args.args[0]

    # on-time job never alerted
    for call in notify_mock.call_args_list:
        assert "trend_snapshots" not in call.args[0]
    # self never alerted
    for call in notify_mock.call_args_list:
        assert "watchdog" not in call.args[0]


# ---------------------------------------------------------------------------
# Test 2 — check_scheduler_stalls debounce: second call → no re-alert
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_scheduler_stalls_debounced():
    """Calling check_scheduler_stalls twice within the cooldown window fires the
    alert only once; the stalled job id is still returned on the second call."""
    from backend.agents import watchdog

    now_utc = _utcnow()
    grace_s = 300
    cooldown_s = 3600

    overdue_job = _fake_job("retry_deliveries", now_utc - timedelta(seconds=grace_s + 120))
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [overdue_job])

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.scheduler.scheduler", fake_scheduler), \
         patch("backend.events.notify_phone", notify_mock):
        # First call — alert should fire
        result1 = await watchdog.check_scheduler_stalls(grace_s=grace_s, cooldown_s=cooldown_s)
        # Second call within cooldown — no re-alert
        result2 = await watchdog.check_scheduler_stalls(grace_s=grace_s, cooldown_s=cooldown_s)

    assert "retry_deliveries" in result1
    assert "retry_deliveries" in result2
    # Alert only fired once despite two calls
    assert notify_mock.call_count == 1


# ---------------------------------------------------------------------------
# Test 3 — check_scheduler_stalls: debounce passes after explicit now bypass
# ---------------------------------------------------------------------------

def test_should_alert_timing():
    """_should_alert returns True when cooldown elapsed, False otherwise.
    Uses explicit now= to control time deterministically.
    """
    from backend.agents.watchdog import _should_alert, reset

    reset()
    t0 = 1000.0
    cooldown = 60.0

    # First call — no prior record, should fire
    assert _should_alert("test_key", cooldown, now=t0) is True
    # Immediately after — should NOT fire (0 elapsed)
    assert _should_alert("test_key", cooldown, now=t0) is False
    # Just before cooldown expires — should NOT fire
    assert _should_alert("test_key", cooldown, now=t0 + 59.0) is False
    # Exactly at cooldown boundary — should fire
    assert _should_alert("test_key", cooldown, now=t0 + 60.0) is True


# ---------------------------------------------------------------------------
# Test 4 — check_dead_letters: threshold logic + debounce
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_dead_letters_threshold_and_alert(eng):
    """Rows at/above threshold trigger an alert; below-threshold rows are ignored.
    The returned count equals the number of qualifying rows only.
    """
    from backend.agents import watchdog
    from backend.database import PendingDelivery

    # Seed: 2 dead-lettered rows (attempts >= 5), 1 below threshold
    with Session(eng) as s:
        s.add(PendingDelivery(payload_json='{"a":1}', delivery_type="notify", attempts=6))
        s.add(PendingDelivery(payload_json='{"b":2}', delivery_type="notify", attempts=5))
        s.add(PendingDelivery(payload_json='{"c":3}', delivery_type="action", attempts=2))
        s.commit()

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.events.notify_phone", notify_mock):
        count = await watchdog.check_dead_letters(threshold=5, cooldown_s=3600)

    assert count == 2
    notify_mock.assert_called_once()
    call_kwargs = notify_mock.call_args.kwargs
    assert call_kwargs["kind"] == "dead_letter"
    assert "2" in notify_mock.call_args.args[0]


@pytest.mark.asyncio
async def test_check_dead_letters_debounced(eng):
    """Second call within cooldown does not re-alert."""
    from backend.agents import watchdog
    from backend.database import PendingDelivery, SystemState

    with Session(eng) as s:
        # Seed SystemState row 1 (production seeds it via _ensure_system_state);
        # the DB-backed debounce reads/writes its last_dead_letter_alert_at field.
        s.add(SystemState(id=1))
        s.add(PendingDelivery(payload_json='{"x":1}', delivery_type="notify", attempts=7))
        s.commit()

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.events.notify_phone", notify_mock):
        count1 = await watchdog.check_dead_letters(threshold=5, cooldown_s=3600)
        count2 = await watchdog.check_dead_letters(threshold=5, cooldown_s=3600)

    assert count1 == 1
    assert count2 == 1
    assert notify_mock.call_count == 1  # debounced — no re-alert


@pytest.mark.asyncio
async def test_check_dead_letters_below_threshold_no_alert(eng):
    """No alert when all rows are below the threshold."""
    from backend.agents import watchdog
    from backend.database import PendingDelivery

    with Session(eng) as s:
        s.add(PendingDelivery(payload_json='{"y":1}', delivery_type="notify", attempts=3))
        s.add(PendingDelivery(payload_json='{"z":2}', delivery_type="action", attempts=4))
        s.commit()

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.events.notify_phone", notify_mock):
        count = await watchdog.check_dead_letters(threshold=5, cooldown_s=3600)

    assert count == 0
    notify_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 5 — run_watchdog disabled: returns {"skipped": True}, no alerts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_watchdog_disabled():
    """When watchdog_enabled=False, run_watchdog returns {"skipped": True}
    and no notify_phone is called."""
    from backend.agents import watchdog
    from backend.config import Settings

    disabled_settings = Settings(watchdog_enabled=False)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=disabled_settings), \
         patch("backend.events.notify_phone", notify_mock):
        result = await watchdog.run_watchdog()

    assert result == {"skipped": True}
    notify_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6 — best-effort: scheduler.get_jobs() raising → [] and no propagation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_scheduler_stalls_get_jobs_raises():
    """If scheduler.get_jobs() raises, check_scheduler_stalls returns [] and
    does not propagate the exception."""
    from backend.agents import watchdog

    exploding_scheduler = SimpleNamespace(get_jobs=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with patch("backend.scheduler.scheduler", exploding_scheduler):
        result = await watchdog.check_scheduler_stalls(grace_s=300, cooldown_s=3600)

    assert result == []


# ---------------------------------------------------------------------------
# Test 7 — run_watchdog never raises even if internals explode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_watchdog_never_raises():
    """run_watchdog catches all exceptions and returns a dict."""
    from backend.agents import watchdog
    from backend.config import Settings

    enabled_settings = Settings(watchdog_enabled=True)

    # Make check_scheduler_stalls raise to exercise the outer try/except
    with patch("backend.config.get_settings", return_value=enabled_settings), \
         patch.object(watchdog, "check_scheduler_stalls", side_effect=RuntimeError("hard crash")):
        result = await watchdog.run_watchdog()

    # Must always return a dict, never raise
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# Test 8 — Scheduler registers "watchdog" job when watchdog_enabled=True
# ---------------------------------------------------------------------------

def test_scheduler_registers_watchdog_job_when_enabled():
    """setup_scheduler adds the 'watchdog' job id when watchdog_enabled=True."""
    from backend.scheduler import setup_scheduler, scheduler

    with patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:00", "America/Detroit")

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "watchdog" in ids


def test_scheduler_omits_watchdog_job_when_disabled():
    """setup_scheduler does NOT add 'watchdog' when watchdog_enabled=False."""
    from backend.scheduler import setup_scheduler, scheduler
    from backend.config import Settings

    disabled_settings = Settings(watchdog_enabled=False)
    with patch("backend.config.get_settings", return_value=disabled_settings), \
         patch.object(scheduler, "add_job") as mock_add:
        setup_scheduler("07:00", "America/Detroit")

    ids = {c.kwargs.get("id") for c in mock_add.call_args_list}
    assert "watchdog" not in ids


# ---------------------------------------------------------------------------
# Test 9 — _watchdog scheduler wrapper calls run_watchdog, swallows exceptions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_watchdog_scheduler_wrapper_calls_run_watchdog():
    """The _watchdog scheduler wrapper calls run_watchdog and does not raise."""
    from backend.scheduler import _watchdog

    run_mock = AsyncMock(return_value={"stalled": [], "dead_letters": 0})

    with patch("backend.agents.watchdog.run_watchdog", run_mock):
        await _watchdog()  # must not raise

    run_mock.assert_called_once()


@pytest.mark.asyncio
async def test_watchdog_scheduler_wrapper_swallows_exception():
    """_watchdog swallows exceptions from run_watchdog."""
    from backend.scheduler import _watchdog

    run_mock = AsyncMock(side_effect=RuntimeError("watchdog crashed"))

    with patch("backend.agents.watchdog.run_watchdog", run_mock):
        await _watchdog()  # must not raise


# ---------------------------------------------------------------------------
# Test 10 — run_watchdog full happy path: stalls + dead-letters both returned
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_watchdog_full_happy_path(eng):
    """run_watchdog runs both checks and returns combined result."""
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import PendingDelivery

    now_utc = _utcnow()
    grace_s = 300
    enabled_settings = Settings(
        watchdog_enabled=True,
        scheduler_stall_grace_s=grace_s,
        dead_letter_attempts=5,
        watchdog_alert_cooldown_s=3600,
    )

    overdue_job = _fake_job("record_uptime", now_utc - timedelta(seconds=grace_s + 30))
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [overdue_job])

    with Session(eng) as s:
        s.add(PendingDelivery(payload_json='{"m":1}', delivery_type="notify", attempts=6))
        s.commit()

    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=enabled_settings), \
         patch("backend.scheduler.scheduler", fake_scheduler), \
         patch("backend.events.notify_phone", notify_mock):
        result = await watchdog.run_watchdog()

    assert "stalled" in result
    assert "record_uptime" in result["stalled"]
    assert result["dead_letters"] == 1
    # Two alerts: one for the stalled job, one for dead letters
    assert notify_mock.call_count == 2
    kinds = {c.kwargs["kind"] for c in notify_mock.call_args_list}
    assert kinds == {"scheduler_stall", "dead_letter"}


# ---------------------------------------------------------------------------
# check_budget_warning (Feature 3 — 80% early-warning)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_budget_warning_fires_once_across_two_runs(eng):
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import SystemState, SpendLog

    with Session(eng) as s:
        s.add(SystemState(id=1, daily_budget_usd=25.0))
        s.add(SpendLog(model="claude-sonnet-4-6", cost_usd=20.0))  # 80% of cap
        s.commit()

    settings = Settings(budget_warn_enabled=True, budget_warn_pct=0.80)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        fired1 = await watchdog.check_budget_warning()
        fired2 = await watchdog.check_budget_warning()

    assert fired1 is True
    assert fired2 is False
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "budget_warn"


@pytest.mark.asyncio
async def test_check_budget_warning_disabled_skips_entirely(eng):
    from backend.agents import watchdog
    from backend.config import Settings

    settings = Settings(budget_warn_enabled=False)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.governor.budget_warning_due") as mock_due, \
         patch("backend.events.notify_phone", notify_mock):
        fired = await watchdog.check_budget_warning()

    assert fired is False
    mock_due.assert_not_called()
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_budget_warning_never_raises_when_governor_throws(eng):
    from backend.agents import watchdog
    from backend.config import Settings

    settings = Settings(budget_warn_enabled=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.governor.budget_warning_due", side_effect=RuntimeError("db down")):
        fired = await watchdog.check_budget_warning()  # must not raise

    assert fired is False


@pytest.mark.asyncio
async def test_run_watchdog_summary_includes_budget_warn_fired(eng):
    from backend.agents import watchdog
    from backend.config import Settings

    settings = Settings(watchdog_enabled=True)
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [])

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.scheduler.scheduler", fake_scheduler), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)):
        result = await watchdog.run_watchdog()

    assert "budget_warn_fired" in result
