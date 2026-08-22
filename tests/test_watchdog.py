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
11. check_integration_contracts alerts only after N consecutive breaching ticks (Feature 1).
12. An integration whose fetch() raises is not a breach and resets its streak.
13. A healthy tick resets a partial breach streak (no premature alert on recovery).
14. contract_canary_enabled=False short-circuits before any fetch() call.
15. Contract-breach alerts carry kind="contract_breach".
16. run_watchdog's summary always includes "contract_breaches", success and exception paths alike.
17. The canary calls the cached fetch(), never bypasses it via __wrapped__.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
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
async def test_watchdog_scheduler_wrapper_reraises_exception():
    """_watchdog logs then re-raises exceptions from run_watchdog so
    APScheduler's error event fires and the failure is visible on Pulse."""
    from backend.scheduler import _watchdog

    run_mock = AsyncMock(side_effect=RuntimeError("watchdog crashed"))

    with patch("backend.agents.watchdog.run_watchdog", run_mock):
        with pytest.raises(RuntimeError, match="watchdog crashed"):
            await _watchdog()


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


# ---------------------------------------------------------------------------
# check_auth_failure_burst (401-burst watchdog)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_auth_failure_burst_fires_once_across_two_runs(eng):
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import SystemState
    from backend.safety import authfail

    with Session(eng) as s:
        s.add(SystemState(id=1))
        s.commit()

    authfail.reset()
    for _ in range(30):
        authfail.record_failure("1.2.3.4", "/api/ha/entities")

    settings = Settings(auth_burst_enabled=True, auth_burst_threshold=25, auth_burst_window_minutes=30)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        paged1 = await watchdog.check_auth_failure_burst()
        paged2 = await watchdog.check_auth_failure_burst()

    assert paged1 == ["1.2.3.4"]
    assert paged2 == []
    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "auth_burst"
    authfail.reset()


@pytest.mark.asyncio
async def test_check_auth_failure_burst_below_threshold_no_alert(eng):
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import SystemState
    from backend.safety import authfail

    with Session(eng) as s:
        s.add(SystemState(id=1))
        s.commit()

    authfail.reset()
    for _ in range(24):
        authfail.record_failure("1.2.3.4", "/api/ha/entities")

    settings = Settings(auth_burst_enabled=True, auth_burst_threshold=25, auth_burst_window_minutes=30)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        paged = await watchdog.check_auth_failure_burst()

    assert paged == []
    notify_mock.assert_not_called()
    authfail.reset()


@pytest.mark.asyncio
async def test_check_auth_failure_burst_disabled_skips_entirely(eng):
    from backend.agents import watchdog
    from backend.config import Settings

    settings = Settings(auth_burst_enabled=False)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.governor.claim_auth_burst_alert") as mock_claim, \
         patch("backend.events.notify_phone", notify_mock):
        paged = await watchdog.check_auth_failure_burst()

    assert paged == []
    mock_claim.assert_not_called()
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_auth_failure_burst_never_raises_when_governor_throws(eng):
    from backend.agents import watchdog
    from backend.config import Settings

    settings = Settings(auth_burst_enabled=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.governor.claim_auth_burst_alert", side_effect=RuntimeError("db down")):
        paged = await watchdog.check_auth_failure_burst()

    assert paged == []


@pytest.mark.asyncio
async def test_auth_burst_message_contains_source_count_and_paths(eng):
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import SystemState
    from backend.safety import authfail

    with Session(eng) as s:
        s.add(SystemState(id=1))
        s.commit()

    authfail.reset()
    for _ in range(30):
        authfail.record_failure("1.2.3.4", "/api/ha/entities")

    settings = Settings(auth_burst_enabled=True, auth_burst_threshold=25, auth_burst_window_minutes=30)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        await watchdog.check_auth_failure_burst()

    body = notify_mock.await_args.args[0]
    assert "1.2.3.4" in body
    assert "30" in body
    assert "/api/ha/entities" in body
    authfail.reset()


@pytest.mark.asyncio
async def test_run_watchdog_summary_includes_auth_bursts(eng):
    from backend.agents import watchdog
    from backend.config import Settings

    settings = Settings(watchdog_enabled=True)
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [])

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.scheduler.scheduler", fake_scheduler), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)):
        result = await watchdog.run_watchdog()

    assert "auth_bursts" in result


# ---------------------------------------------------------------------------
# check_integration_contracts (Feature 1 — Integration Contract Canary)
#
# All tests patch contracts.CONTRACTS down to a single fake "weather" entry
# and patch backend.integrations.weather.fetch directly (the real module
# object, resolved via the same import_module path the canary itself uses —
# no real network/integration is ever touched).
# ---------------------------------------------------------------------------

@dataclass
class _FakeWeatherData:
    condition: str = "Sunny"


def _breaching_contracts():
    from backend.safety.contracts import FieldContract
    return {
        "weather": (
            FieldContract("condition", (str,), "not_default", default="Unknown", consumer="test:1"),
        ),
    }


_BREACHING_CONTRACTS = _breaching_contracts()


def _settings_with_canary(**overrides):
    from backend.config import Settings
    defaults = dict(watchdog_enabled=True, contract_canary_enabled=True, contract_canary_consecutive_ticks=3)
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_contract_canary_no_alert_below_streak():
    from backend.agents import watchdog

    settings = _settings_with_canary()
    notify_mock = AsyncMock(return_value=True)
    breaching = AsyncMock(return_value=_FakeWeatherData(condition="Unknown"))

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.contracts.CONTRACTS", _BREACHING_CONTRACTS), \
         patch("backend.integrations.weather.fetch", breaching), \
         patch("backend.events.notify_phone", notify_mock):
        paged1 = await watchdog.check_integration_contracts()
        paged2 = await watchdog.check_integration_contracts()
        paged3 = await watchdog.check_integration_contracts()

    assert paged1 == [] and paged2 == []
    assert paged3 == ["weather"]
    notify_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_contract_canary_exception_is_not_a_breach():
    from backend.agents import watchdog

    settings = _settings_with_canary()
    notify_mock = AsyncMock(return_value=True)
    raising = AsyncMock(side_effect=RuntimeError("integration down"))

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.contracts.CONTRACTS", _BREACHING_CONTRACTS), \
         patch("backend.integrations.weather.fetch", raising), \
         patch("backend.events.notify_phone", notify_mock):
        for _ in range(5):
            paged = await watchdog.check_integration_contracts()
            assert paged == []

    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_contract_canary_streak_resets_on_recovery():
    from backend.agents import watchdog

    settings = _settings_with_canary()
    notify_mock = AsyncMock(return_value=True)
    breaching = _FakeWeatherData(condition="Unknown")
    healthy = _FakeWeatherData(condition="Sunny")

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.contracts.CONTRACTS", _BREACHING_CONTRACTS), \
         patch("backend.events.notify_phone", notify_mock):
        for data in (breaching, breaching, healthy, breaching, breaching):
            with patch("backend.integrations.weather.fetch", AsyncMock(return_value=data)):
                paged = await watchdog.check_integration_contracts()
                assert paged == []

    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_contract_canary_disabled_flag():
    from backend.agents import watchdog

    settings = _settings_with_canary(contract_canary_enabled=False)
    fetch_mock = AsyncMock(return_value=_FakeWeatherData(condition="Unknown"))

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.contracts.CONTRACTS", _BREACHING_CONTRACTS), \
         patch("backend.integrations.weather.fetch", fetch_mock):
        paged = await watchdog.check_integration_contracts()

    assert paged == []
    fetch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_contract_canary_alert_kind():
    from backend.agents import watchdog

    settings = _settings_with_canary()
    notify_mock = AsyncMock(return_value=True)
    breaching = AsyncMock(return_value=_FakeWeatherData(condition="Unknown"))

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.contracts.CONTRACTS", _BREACHING_CONTRACTS), \
         patch("backend.integrations.weather.fetch", breaching), \
         patch("backend.events.notify_phone", notify_mock):
        for _ in range(3):
            await watchdog.check_integration_contracts()

    assert notify_mock.await_args.kwargs["kind"] == "contract_breach"


@pytest.mark.asyncio
async def test_run_watchdog_includes_contract_breaches_key(eng):
    from backend.agents import watchdog

    settings = _settings_with_canary()
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [])

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.scheduler.scheduler", fake_scheduler), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)):
        result = await watchdog.run_watchdog()
    assert "contract_breaches" in result

    # Also present in the outer-exception fallback path.
    with patch("backend.config.get_settings", side_effect=RuntimeError("boom")):
        result2 = await watchdog.run_watchdog()
    assert "contract_breaches" in result2


# ---------------------------------------------------------------------------
# Outcome-flag write path (docs/outcome-tracker-spec.md AC21)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ac21_flagging_checks_call_record_flag_budget_warn_does_not(eng):
    """AC21: each of the four flagging checks (check_scheduler_stalls,
    check_dead_letters, check_auth_failure_burst, check_integration_contracts)
    calls record_flag_ex with its documented (source, check) fingerprint
    before notify_phone; check_budget_warning calls it zero times.

    Updated for the record_flag -> record_flag_ex conversion (mirrors cycle
    7's homelab_watch.py precedent, commit 224a402): the mock now patches
    record_flag_ex directly (the four call sites no longer go through the
    record_flag back-compat wrapper) and returns a surface=True dict so the
    `if d["surface"]:` gate still lets notify_phone fire, matching this
    test's pre-existing, unrelated-to-CAL28/29 behavior."""
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import PendingDelivery, SystemState, SpendLog
    from backend.safety import authfail

    with Session(eng) as s:
        s.add(SystemState(id=1, daily_budget_usd=25.0))
        s.add(PendingDelivery(payload_json='{"a":1}', delivery_type="notify", attempts=6))
        s.add(SpendLog(model="claude-sonnet-4-6", cost_usd=20.0))
        s.commit()

    now_utc = _utcnow()
    overdue_job = _fake_job("morning_briefing", now_utc - timedelta(seconds=600))
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [overdue_job])

    authfail.reset()
    for _ in range(30):
        authfail.record_failure("1.2.3.4", "/api/ha/entities")

    settings = Settings(
        watchdog_enabled=True,
        auth_burst_enabled=True, auth_burst_threshold=25, auth_burst_window_minutes=30,
        contract_canary_enabled=True, contract_canary_consecutive_ticks=1,
        budget_warn_enabled=True, budget_warn_pct=0.80,
    )

    record_flag_ex_mock = AsyncMock(return_value={"id": 1, "surface": True, "reason": None})
    breaching_fetch = AsyncMock(return_value=_FakeWeatherData(condition="Unknown"))

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.scheduler.scheduler", fake_scheduler), \
         patch("backend.safety.contracts.CONTRACTS", _BREACHING_CONTRACTS), \
         patch("backend.integrations.weather.fetch", breaching_fetch), \
         patch("backend.agents.outcomes.record_flag_ex", record_flag_ex_mock), \
         patch("backend.events.notify_phone", new_callable=AsyncMock, return_value=True):
        await watchdog.check_scheduler_stalls(grace_s=300, cooldown_s=3600)
        await watchdog.check_dead_letters(threshold=5, cooldown_s=3600)
        await watchdog.check_auth_failure_burst()
        await watchdog.check_integration_contracts()  # consecutive_ticks=1 -> fires this tick
        await watchdog.check_budget_warning()

    authfail.reset()

    calls = {(c.args[0], c.args[1]) for c in record_flag_ex_mock.await_args_list}
    assert ("watchdog", "stall:morning_briefing") in calls
    assert ("watchdog", "dead_letters") in calls
    assert ("watchdog", "auth_burst:1.2.3.4") in calls
    assert ("contracts", "breach:weather") in calls
    assert record_flag_ex_mock.await_count == 4  # check_budget_warning contributed none


@pytest.mark.asyncio
async def test_cal28_dead_letters_high_severity_hint_still_pages_by_default(eng):
    """CAL28 (docs/calibration-loop-spec.md 8.5): with an active, 100%-FP
    calibration hint for watchdog:dead_letters and calibration_suppression_
    enabled=True, the shipped DEFAULT calibration_suppress_high_severity=
    False means check_dead_letters's severity="high" record_flag_ex call
    still returns surface=True -- the row is written (suppressed=False) and
    events.notify_phone still fires. The high-severity guardrail exists so a
    single active hint can never silently blind a high-severity page unless
    an operator explicitly opts in (CAL29 covers the opt-in)."""
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import CalibrationHint, OutcomeFlag, PendingDelivery, SystemState

    with Session(eng) as s:
        s.add(SystemState(id=1))
        s.add(PendingDelivery(payload_json='{"a":1}', delivery_type="notify", attempts=6))
        s.add(CalibrationHint(
            fingerprint="watchdog:dead_letters",
            status="active", verdict_count=20, false_positive_count=20, fp_rate=1.0,
        ))
        s.commit()

    settings = Settings(
        calibration_enabled=True, calibration_suppression_enabled=True,
        calibration_suppress_high_severity=False,  # shipped default
    )
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        count = await watchdog.check_dead_letters(threshold=5, cooldown_s=3600)

    assert count == 1
    notify_mock.assert_awaited_once()  # CAL28 -- still pages through the active hint

    with Session(eng) as s:
        row = s.exec(
            select(OutcomeFlag).where(OutcomeFlag.fingerprint == "watchdog:dead_letters")
        ).one()
    assert row.suppressed is False


@pytest.mark.asyncio
async def test_cal29_dead_letters_high_severity_hint_suppressed_when_opted_in(eng):
    """CAL29: the same active hint as CAL28, but with
    calibration_suppress_high_severity explicitly set True (opt-in) -- now
    check_dead_letters's record_flag_ex call returns surface=False, so
    events.notify_phone is NOT called, while the OutcomeFlag row is still
    written and stamped suppressed=True with a non-empty suppressed_reason
    (the write/page split, spec docs/outcome-tracker-spec.md §3.1, is
    preserved through the record_flag -> record_flag_ex conversion)."""
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import CalibrationHint, OutcomeFlag, PendingDelivery, SystemState

    with Session(eng) as s:
        s.add(SystemState(id=1))
        s.add(PendingDelivery(payload_json='{"a":1}', delivery_type="notify", attempts=6))
        s.add(CalibrationHint(
            fingerprint="watchdog:dead_letters",
            status="active", verdict_count=20, false_positive_count=20, fp_rate=1.0,
        ))
        s.commit()

    settings = Settings(
        calibration_enabled=True, calibration_suppression_enabled=True,
        calibration_suppress_high_severity=True,  # explicit opt-in
    )
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        count = await watchdog.check_dead_letters(threshold=5, cooldown_s=3600)

    assert count == 1
    notify_mock.assert_not_called()  # CAL29 -- page suppressed by opt-in guardrail

    with Session(eng) as s:
        row = s.exec(
            select(OutcomeFlag).where(OutcomeFlag.fingerprint == "watchdog:dead_letters")
        ).one()
    assert row.suppressed is True
    assert row.suppressed_reason


@pytest.mark.asyncio
async def test_contract_canary_does_not_bypass_cache():
    from backend.agents import watchdog

    settings = _settings_with_canary()
    real_fetch = AsyncMock(return_value=_FakeWeatherData(condition="Sunny"))
    wrapped_original = AsyncMock(side_effect=AssertionError("must not call __wrapped__"))
    real_fetch.__wrapped__ = wrapped_original

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.safety.contracts.CONTRACTS", _BREACHING_CONTRACTS), \
         patch("backend.integrations.weather.fetch", real_fetch):
        await watchdog.check_integration_contracts()

    real_fetch.assert_awaited_once()
    wrapped_original.assert_not_called()


# ---------------------------------------------------------------------------
# check_deferred_flags / deferred_swept (rollout step 7, spec §3.5, AC29/AC30)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_deferred_flags_pages_once_per_flipped_id(eng):
    """A past-due deferred flag is flipped by sweep_deferred() and paged exactly
    once via notify_phone(kind="flag_followup") with resolved/false_positive
    buttons keyed to its id; a future-dated deferred flag is left alone and
    never paged."""
    from backend.agents import watchdog
    from backend.config import Settings
    from backend.database import OutcomeFlag

    now = datetime.utcnow()
    with Session(eng) as s:
        due = OutcomeFlag(
            source="homelab_watch", check="garage_open", summary="Garage door open",
            status="deferred", deferred_until=now - timedelta(minutes=5),
        )
        future = OutcomeFlag(
            source="homelab_watch", check="stale_prs", summary="PR stale",
            status="deferred", deferred_until=now + timedelta(hours=1),
        )
        s.add(due)
        s.add(future)
        s.commit()
        due_id = due.id

    settings = Settings(outcome_flag_sweep_enabled=True)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        ids = await watchdog.check_deferred_flags()

    assert ids == [due_id]
    notify_mock.assert_awaited_once()
    call = notify_mock.await_args
    assert call.kwargs["kind"] == "flag_followup"
    callback_datas = {b["callback_data"] for b in call.kwargs["buttons"]}
    assert callback_datas == {f"flag:resolved:{due_id}", f"flag:false_positive:{due_id}"}

    with Session(eng) as s:
        rows = {r.id: r.status for r in s.exec(select(OutcomeFlag)).all()}
    assert rows[due_id] == "needs_follow_up"


@pytest.mark.asyncio
async def test_check_deferred_flags_disabled_skips_sweep(eng):
    """outcome_flag_sweep_enabled=False short-circuits before sweep_deferred()
    is even called — independent of watchdog_enabled."""
    from backend.agents import watchdog
    from backend.config import Settings

    settings = Settings(outcome_flag_sweep_enabled=False)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.outcomes.sweep_deferred") as sweep_mock, \
         patch("backend.events.notify_phone", notify_mock):
        ids = await watchdog.check_deferred_flags()

    assert ids == []
    sweep_mock.assert_not_called()
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_watchdog_includes_deferred_swept_key(eng):
    """run_watchdog's summary dict gains 'deferred_swept' in both the happy
    path and the outer-exception fallback (AC30)."""
    from backend.agents import watchdog

    settings = _settings_with_canary()
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [])

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.scheduler.scheduler", fake_scheduler), \
         patch.object(watchdog, "check_deferred_flags", AsyncMock(return_value=[7])), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)):
        result = await watchdog.run_watchdog()
    assert result["deferred_swept"] == [7]
    # Every pre-existing key must still be present alongside the new one.
    assert {"stalled", "dead_letters", "budget_warn_fired", "auth_bursts", "contract_breaches"} <= result.keys()

    # Also present (as []) in the outer-exception fallback path.
    with patch("backend.config.get_settings", side_effect=RuntimeError("boom")):
        result2 = await watchdog.run_watchdog()
    assert result2["deferred_swept"] == []
    assert {"stalled", "dead_letters", "budget_warn_fired", "auth_bursts", "contract_breaches"} <= result2.keys()


# ---------------------------------------------------------------------------
# check_deploy_drift (Task 1 — deploy-drift check, spec §1.5)
# ---------------------------------------------------------------------------

def _drift_settings(**overrides):
    from backend.config import Settings
    defaults = dict(watchdog_enabled=True, deploy_drift_check_enabled=True,
                     watchdog_alert_cooldown_s=3600)
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_check_deploy_drift_no_drift_same_sha():
    from backend.agents import watchdog

    sha = "a" * 40
    settings = _drift_settings()
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=sha), \
         patch("backend.version.get_git_head", return_value=sha), \
         patch("backend.events.notify_phone", notify_mock):
        fired = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired is False
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_deploy_drift_detected_alerts_with_both_shas(eng):
    from backend.agents import watchdog

    running = "a" * 40
    current = "b" * 40
    settings = _drift_settings()
    notify_mock = AsyncMock(return_value=True)
    record_flag_ex_mock = AsyncMock(return_value={"id": 1, "surface": True, "reason": None})

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=running), \
         patch("backend.version.get_git_head", return_value=current), \
         patch("backend.agents.outcomes.record_flag_ex", record_flag_ex_mock), \
         patch("backend.events.notify_phone", notify_mock):
        fired = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired is True
    record_flag_ex_mock.assert_awaited_once()
    assert record_flag_ex_mock.await_args.args[0] == "watchdog"
    assert record_flag_ex_mock.await_args.args[1] == "deploy_drift"
    assert record_flag_ex_mock.await_args.kwargs["severity"] == "medium"

    notify_mock.assert_awaited_once()
    assert notify_mock.await_args.kwargs["kind"] == "deploy_drift"
    body = notify_mock.await_args.args[0]
    assert running[:12] in body
    assert current[:12] in body


@pytest.mark.asyncio
async def test_check_deploy_drift_suppressed_flag_still_returns_true():
    """A calibration-suppressed record_flag_ex (surface=False) still means
    drift WAS detected on this tick -- check_deploy_drift returns True either
    way; only the phone page is gated on d['surface']."""
    from backend.agents import watchdog

    running = "a" * 40
    current = "b" * 40
    settings = _drift_settings()
    notify_mock = AsyncMock(return_value=True)
    record_flag_ex_mock = AsyncMock(return_value={"id": 1, "surface": False, "reason": "suppressed"})

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=running), \
         patch("backend.version.get_git_head", return_value=current), \
         patch("backend.agents.outcomes.record_flag_ex", record_flag_ex_mock), \
         patch("backend.events.notify_phone", notify_mock):
        fired = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired is True
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_deploy_drift_cooldown_single_page_across_two_calls():
    from backend.agents import watchdog

    running = "a" * 40
    current = "b" * 40
    settings = _drift_settings()
    notify_mock = AsyncMock(return_value=True)
    record_flag_ex_mock = AsyncMock(return_value={"id": 1, "surface": True, "reason": None})

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=running), \
         patch("backend.version.get_git_head", return_value=current), \
         patch("backend.agents.outcomes.record_flag_ex", record_flag_ex_mock), \
         patch("backend.events.notify_phone", notify_mock):
        fired1 = await watchdog.check_deploy_drift(cooldown_s=3600)
        fired2 = await watchdog.check_deploy_drift(cooldown_s=3600)

    # Drift is still "detected" on both calls...
    assert fired1 is True
    assert fired2 is True
    # ...but the phone page (and the flag write it gates on) only fires once.
    notify_mock.assert_called_once()
    record_flag_ex_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_deploy_drift_unknown_running_sha_noop():
    from backend.agents import watchdog

    settings = _drift_settings()
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=None), \
         patch("backend.version.get_git_head", return_value="a" * 40), \
         patch("backend.events.notify_phone", notify_mock):
        fired = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired is False
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_deploy_drift_unknown_current_sha_noop():
    from backend.agents import watchdog

    settings = _drift_settings()
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value="a" * 40), \
         patch("backend.version.get_git_head", return_value=None), \
         patch("backend.events.notify_phone", notify_mock):
        fired = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired is False
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_deploy_drift_disabled_flag_noop():
    from backend.agents import watchdog

    settings = _drift_settings(deploy_drift_check_enabled=False)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value="a" * 40), \
         patch("backend.version.get_git_head", return_value="b" * 40), \
         patch("backend.events.notify_phone", notify_mock):
        fired = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired is False
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_watchdog_includes_deploy_drift_key(eng):
    from backend.agents import watchdog

    settings = _drift_settings()
    fake_scheduler = SimpleNamespace(get_jobs=lambda: [])

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.scheduler.scheduler", fake_scheduler), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)):
        result = await watchdog.run_watchdog()
    assert "deploy_drift" in result


# ---------------------------------------------------------------------------
# check_deploy_drift auto-restart escalation (deploy_drift_autorestart_enabled)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_deploy_drift_first_tick_no_autorestart(eng):
    """First consecutive drift tick only alerts, exactly like today -- the
    auto-restart escalation never fires on tick 1, even with the flag on."""
    from backend.agents import watchdog

    running = "a" * 40
    current = "b" * 40
    settings = _drift_settings(deploy_drift_autorestart_enabled=True)
    execute_mock = AsyncMock()

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=running), \
         patch("backend.version.get_git_head", return_value=current), \
         patch("backend.agents.outcomes.record_flag_ex",
               AsyncMock(return_value={"id": 1, "surface": True, "reason": None})), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)), \
         patch("backend.safety.broker.execute_action", execute_mock):
        fired = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired is True
    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_deploy_drift_second_tick_autorestarts_when_enabled(eng):
    """2nd CONSECUTIVE drift tick, flag on, kill switch on (default
    SystemState.autonomy_enabled) -> broker.execute_action dispatches the
    same system_restart/lxc target the manual button uses, self-confirmed."""
    from backend.agents import watchdog

    running = "a" * 40
    current = "b" * 40
    settings = _drift_settings(deploy_drift_autorestart_enabled=True)
    execute_mock = AsyncMock()

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=running), \
         patch("backend.version.get_git_head", return_value=current), \
         patch("backend.agents.outcomes.record_flag_ex",
               AsyncMock(return_value={"id": 1, "surface": True, "reason": None})), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)), \
         patch("backend.safety.broker.execute_action", execute_mock):
        await watchdog.check_deploy_drift(cooldown_s=3600)
        fired2 = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired2 is True
    execute_mock.assert_awaited_once()
    assert execute_mock.await_args.kwargs["actor"] == "autonomous"
    assert execute_mock.await_args.kwargs["kind"] == "system_restart"
    assert execute_mock.await_args.kwargs["target"] == "lxc"
    assert execute_mock.await_args.kwargs["confirmed"] is True


@pytest.mark.asyncio
async def test_check_deploy_drift_second_tick_no_autorestart_when_flag_disabled(eng):
    """Flag stays default-off -> the 2nd consecutive tick still only alerts,
    matching today's behavior byte for byte."""
    from backend.agents import watchdog

    running = "a" * 40
    current = "b" * 40
    settings = _drift_settings()  # deploy_drift_autorestart_enabled defaults False
    execute_mock = AsyncMock()

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=running), \
         patch("backend.version.get_git_head", return_value=current), \
         patch("backend.agents.outcomes.record_flag_ex",
               AsyncMock(return_value={"id": 1, "surface": True, "reason": None})), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)), \
         patch("backend.safety.broker.execute_action", execute_mock):
        await watchdog.check_deploy_drift(cooldown_s=3600)
        await watchdog.check_deploy_drift(cooldown_s=3600)

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_deploy_drift_kill_switch_off_prevents_autorestart(eng):
    """Flag on but the global kill switch (SystemState.autonomy_enabled) is
    off -> the broker's own kill-switch check (BEFORE classify/decide,
    unconditional on confirmed=True) FORBIDs the call outright and the
    restart is never actually dispatched. Runs the REAL broker, not a mock,
    to prove the enforcement this call site relies on instead of assuming it."""
    from backend.database import SystemState
    from backend.agents import watchdog

    with Session(eng) as session:
        session.add(SystemState(id=1, autonomy_enabled=False))
        session.commit()

    running = "a" * 40
    current = "b" * 40
    settings = _drift_settings(deploy_drift_autorestart_enabled=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.version.running_sha", return_value=running), \
         patch("backend.version.get_git_head", return_value=current), \
         patch("backend.agents.outcomes.record_flag_ex",
               AsyncMock(return_value={"id": 1, "surface": True, "reason": None})), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)), \
         patch("subprocess.run") as subprocess_run_mock:
        await watchdog.check_deploy_drift(cooldown_s=3600)
        fired2 = await watchdog.check_deploy_drift(cooldown_s=3600)

    assert fired2 is True
    subprocess_run_mock.assert_not_called()

    from backend.database import ActionLog
    with Session(eng) as session:
        rows = session.exec(
            select(ActionLog).where(ActionLog.kind == "system_restart")
        ).all()
    assert len(rows) == 1
    assert rows[0].decision == "forbidden"


@pytest.mark.asyncio
async def test_check_deploy_drift_streak_resets_when_drift_clears(eng):
    """The consecutive-tick counter resets to 0 the moment a tick observes no
    drift -- a later new drift incident starts back at tick 1, so the
    auto-restart escalation only fires on ITS 2nd consecutive tick, not by
    accumulating across an intervening clear."""
    from backend.agents import watchdog

    same = "a" * 40
    running = "a" * 40
    current = "b" * 40
    settings = _drift_settings(deploy_drift_autorestart_enabled=True)
    execute_mock = AsyncMock()

    sha_pairs = [
        (running, current),  # tick 1: drift (streak 1)
        (same, same),        # tick 2: clears (streak resets to 0)
        (running, current),  # tick 3: drift again (streak 1, NOT 3)
        (running, current),  # tick 4: drift again (streak 2 -> autorestart)
    ]

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.outcomes.record_flag_ex",
               AsyncMock(return_value={"id": 1, "surface": True, "reason": None})), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)), \
         patch("backend.safety.broker.execute_action", execute_mock):
        for run_sha, cur_sha in sha_pairs:
            with patch("backend.version.running_sha", return_value=run_sha), \
                 patch("backend.version.get_git_head", return_value=cur_sha):
                await watchdog.check_deploy_drift(cooldown_s=3600)

    execute_mock.assert_awaited_once()

    # Also present in the outer-exception fallback path.
    with patch("backend.config.get_settings", side_effect=RuntimeError("boom")):
        result2 = await watchdog.run_watchdog()
    assert result2["deploy_drift"] is False


# ---------------------------------------------------------------------------
# check_expected_deliveries (expected-delivery heartbeat check — 8th check)
# ---------------------------------------------------------------------------

def _delivery_settings(**overrides):
    from backend.config import Settings
    defaults = dict(watchdog_enabled=True, expected_delivery_check_enabled=True,
                     watchdog_alert_cooldown_s=3600)
    defaults.update(overrides)
    return Settings(**defaults)


def _seed_delivery(eng, name, *, last_heartbeat_at, interval_minutes=60, grace_minutes=30):
    from backend.database import ExpectedDelivery
    with Session(eng) as s:
        s.add(ExpectedDelivery(
            name=name,
            expected_interval_minutes=interval_minutes,
            grace_minutes=grace_minutes,
            last_heartbeat_at=last_heartbeat_at,
        ))
        s.commit()


@pytest.mark.asyncio
async def test_check_expected_deliveries_stale_pages_once(eng):
    """A delivery overdue past interval+grace pages, and a repeat call within
    the cooldown window does not re-page (same _should_alert discipline as
    every other watchdog check)."""
    from backend.agents import watchdog

    _seed_delivery(
        eng, "brain_organizer",
        last_heartbeat_at=datetime.utcnow() - timedelta(minutes=200),
        interval_minutes=60, grace_minutes=30,
    )
    settings = _delivery_settings()
    notify_mock = AsyncMock(return_value=True)
    record_flag_ex_mock = AsyncMock(return_value={"id": 1, "surface": True, "reason": None})

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.agents.outcomes.record_flag_ex", record_flag_ex_mock), \
         patch("backend.events.notify_phone", notify_mock):
        paged1 = await watchdog.check_expected_deliveries(cooldown_s=3600)
        paged2 = await watchdog.check_expected_deliveries(cooldown_s=3600)

    assert paged1 == ["brain_organizer"]
    assert paged2 == []  # still overdue, but the cooldown suppresses a repeat page.
    notify_mock.assert_awaited_once()
    record_flag_ex_mock.assert_awaited_once()
    assert record_flag_ex_mock.await_args.args[0] == "watchdog"
    assert record_flag_ex_mock.await_args.args[1] == "stale_delivery:brain_organizer"
    assert record_flag_ex_mock.await_args.kwargs["severity"] == "high"
    assert notify_mock.await_args.kwargs["kind"] == "stale_delivery"


@pytest.mark.asyncio
async def test_check_expected_deliveries_fresh_never_pages(eng):
    """A delivery whose last heartbeat is well within interval+grace never
    pages."""
    from backend.agents import watchdog

    _seed_delivery(
        eng, "morning_briefing",
        last_heartbeat_at=datetime.utcnow() - timedelta(minutes=5),
        interval_minutes=1440, grace_minutes=120,
    )
    settings = _delivery_settings()
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        paged = await watchdog.check_expected_deliveries(cooldown_s=3600)

    assert paged == []
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_expected_deliveries_no_registered_deliveries_noop(eng):
    """No ExpectedDelivery rows at all -> no-op, no error."""
    from backend.agents import watchdog

    settings = _delivery_settings()
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        paged = await watchdog.check_expected_deliveries(cooldown_s=3600)

    assert paged == []
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_check_expected_deliveries_disabled_flag_noop(eng):
    from backend.agents import watchdog

    _seed_delivery(
        eng, "brain_organizer",
        last_heartbeat_at=datetime.utcnow() - timedelta(minutes=200),
        interval_minutes=60, grace_minutes=30,
    )
    settings = _delivery_settings(expected_delivery_check_enabled=False)
    notify_mock = AsyncMock(return_value=True)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.events.notify_phone", notify_mock):
        paged = await watchdog.check_expected_deliveries(cooldown_s=3600)

    assert paged == []
    notify_mock.assert_not_called()


@pytest.mark.asyncio
async def test_run_watchdog_includes_stale_deliveries_key(eng):
    from backend.agents import watchdog

    settings = _delivery_settings(dead_letter_attempts=5)

    with patch("backend.config.get_settings", return_value=settings), \
         patch("backend.scheduler.scheduler", SimpleNamespace(get_jobs=lambda: [])), \
         patch("backend.events.notify_phone", AsyncMock(return_value=True)):
        result = await watchdog.run_watchdog()

    assert result["stale_deliveries"] == []

    # Also present (as []) in the outer-exception fallback path.
    with patch("backend.config.get_settings", side_effect=RuntimeError("boom")):
        result2 = await watchdog.run_watchdog()
    assert result2["stale_deliveries"] == []
